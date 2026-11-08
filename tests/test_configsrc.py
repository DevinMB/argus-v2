import base64
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from test_auth import sign_up

from argus import configsrc
from argus.app import create_app
from argus.deploys import WRITE_TOKEN
from argus.github import Poller
from argus.runtime import Runtime

LIKE_HOMELAB = """\
# Argus checks. Comments like this one must survive every edit.
sites:
  B: Home
  A: Remote site

checks:
  # ---- apps ----
  - {name: shop, kind: http, target: "https://shop.example/", site: B, group: apps, interval: 30}
  - {name: notes, kind: http, target: "http://10.0.0.5:8080/", site: B, group: apps}
  # ---- infra ----
  - {name: nas, kind: ping, target: 10.0.0.2, site: A, group: infra}

heartbeats:
  - {name: stack-sync, max_age: 900, site: B}  # every 5 minutes
"""
PAT = "github_pat_" + "W" * 30


def test_an_added_check_goes_after_its_group_and_nothing_else_moves():
    new = configsrc.upsert(LIKE_HOMELAB, "checks", {
        "name": "blog", "kind": "http", "target": "https://blog.example/", "group": "apps",
        "site": "B"})
    old, out = LIKE_HOMELAB.splitlines(), new.splitlines()
    assert len(out) == len(old) + 1
    added = [line for line in out if line not in old]
    assert added == [
        '  - {name: blog, kind: http, target: "https://blog.example/", site: B, group: apps}']
    assert out.index(added[0]) == old.index("  # ---- infra ----")  # after the last app
    assert "# every 5 minutes" in new and "# ---- apps ----" in new


def test_replace_remove_and_refusals_leave_the_text_valid():
    out = configsrc.upsert(LIKE_HOMELAB, "checks", {
        "name": "notes", "kind": "http", "target": "http://10.0.0.5:8080/healthz", "site": "B",
        "group": "apps", "interval": 30}, replace="notes")
    assert 'target: "http://10.0.0.5:8080/healthz", site: B, group: apps, interval: 30}' in out
    assert "nas" not in configsrc.remove(LIKE_HOMELAB, "checks", "nas").split("checks:")[1].split(
        "heartbeats")[0]
    with pytest.raises(configsrc.EditError, match="already a check named shop"):
        configsrc.upsert(LIKE_HOMELAB, "checks", {"name": "shop", "kind": "ping", "target": "h",
                                                  "site": "B"})
    with pytest.raises(configsrc.EditError, match="unknown site Z"):
        configsrc.upsert(LIKE_HOMELAB, "checks", {"name": "x", "kind": "ping", "target": "h",
                                                  "site": "Z"})
    with pytest.raises(configsrc.EditError, match="no check named gone"):
        configsrc.upsert(LIKE_HOMELAB, "checks", {"name": "x", "kind": "ping", "target": "h",
                                                  "site": "B"}, replace="gone")


@pytest.fixture
def fresh(tmp_path, prober, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    rt = Runtime(tmp_path / "absent.yml", tmp_path / "argus.db", prober=prober)
    with TestClient(create_app(rt, start_scheduler=False)) as c:
        yield c, {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}


def test_a_fresh_install_edits_its_config_in_argus(fresh):
    c, csrf = fresh
    view = c.get("/api/config", headers=csrf).json()
    assert view["source"] == "argus" and view["editable"]
    r = c.post("/api/config/entries", headers=csrf, json={
        "entry": {"name": "router", "kind": "ping", "target": "192.168.1.1"},
        "base": view["base"]})
    assert r.status_code == 200, r.text
    assert [x["name"] for x in c.get("/api/checks").json()] == ["router"]
    # An edit from an older view is refused, with the newer config to redo it on.
    stale = c.post("/api/config/entries", headers=csrf, json={
        "entry": {"name": "nas", "kind": "ping", "target": "nas"}, "base": view["base"]})
    assert stale.status_code == 409 and "changed since you opened it" in stale.json()["detail"]
    view = c.get("/api/config", headers=csrf).json()
    bad = c.put("/api/config", headers=csrf, json={"text": "checks: [{name: x}]",
                                                     "base": view["base"]})
    assert bad.status_code == 400 and "not saved" in bad.json()["detail"]
    assert [x["name"] for x in c.get("/api/checks").json()] == ["router"]  # still running
    r = c.delete(f"/api/config/entries/checks/router?base={view['base']}", headers=csrf)
    assert r.status_code == 200 and c.get("/api/checks").json() == []
    assert c.get("/api/config/export.yml", headers=csrf).text.startswith("# Argus's config")


def test_a_mounted_file_is_read_only_in_the_ui(tmp_path, prober, caplog, config_file):
    caplog.set_level(logging.WARNING, logger="argus")
    rt = Runtime(config_file, tmp_path / "argus.db", prober=prober)
    with TestClient(create_app(rt, start_scheduler=False)) as c:
        csrf = {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}
        view = c.get("/api/config", headers=csrf).json()
        assert view["source"] == "file" and not view["editable"]
        r = c.post("/api/config/entries", headers=csrf, json={
            "entry": {"name": "x", "kind": "ping", "target": "h", "site": "A"},
            "base": view["base"]})
        assert r.status_code == 409 and "mounted file" in r.json()["detail"]


class FakeRepo:
    """homelab-ops on GitHub: one file, commits and PRs."""

    def __init__(self):
        self.text, self.sha, self.writes = LIKE_HOMELAB, "s1", []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        if m != "GET" or path in ("/repos/ann/ops", "/repos/ann/ops/git/ref/heads/main"):
            if m != "GET":
                assert request.headers["authorization"] == f"Bearer {PAT}"
        if path == "/repos/ann/ops":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/ann/ops/contents/argus/argus.yml":
            if m == "GET":
                return httpx.Response(200, json={
                    "sha": self.sha, "content": base64.b64encode(self.text.encode()).decode()})
            body = json.loads(request.content)
            self.writes.append(("PUT", body["branch"], body["message"]))
            if body["sha"] != self.sha:
                return httpx.Response(409, json={"message": "sha mismatch"})
            if body["branch"] == "main":
                self.text, self.sha = base64.b64decode(body["content"]).decode(), "s2"
            return httpx.Response(200, json={"content": {"sha": "s2"},
                                             "commit": {"html_url": "https://github.com/c/1"}})
        if path == "/repos/ann/ops/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "h1"}})
        if path == "/repos/ann/ops/git/refs":
            self.writes.append(("REF", json.loads(request.content)["ref"], ""))
            return httpx.Response(201, json={})
        if path == "/repos/ann/ops/pulls":
            self.writes.append(("PR", json.loads(request.content)["title"], ""))
            return httpx.Response(201, json={"html_url": "https://github.com/ann/ops/pull/7"})
        return httpx.Response(404, json={})


async def test_git_mode_reads_the_repo_and_saves_as_a_commit_or_a_pr(tmp_path, prober, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    fake = FakeRepo()
    rt = Runtime(tmp_path / "absent.yml", tmp_path / "argus.db", prober=prober)
    poller = Poller("read", transport=httpx.MockTransport(fake))
    with TestClient(create_app(rt, start_scheduler=False, poller=poller)) as c:
        csrf = {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}
        c.app.state.settings.set_secret(WRITE_TOKEN, PAT)
        view = c.put("/api/config/source", headers=csrf, json={
            "source": "github", "repo": "ann/ops", "path": "argus/argus.yml"}).json()
        assert view["source"] == "github" and "# ---- apps ----" in view["text"]
        assert [x["name"] for x in c.get("/api/checks").json()] == ["shop", "notes", "nas"]

        r = c.post("/api/config/entries", headers=csrf, json={
            "entry": {"name": "blog", "kind": "ping", "target": "blog", "site": "B",
                      "group": "apps"}, "base": view["base"]}).json()
        assert r["saved"] == "commit" and fake.writes[-1] == (
            "PUT", "main", "argus.yml: add check blog\n\nEdited in Argus.")
        assert "blog" in fake.text and "# every 5 minutes" in fake.text

        # Someone pushed meanwhile: the save is refused, nothing is lost.
        fake.sha = "s3"
        r = c.post("/api/config/entries", headers=csrf, json={
            "entry": {"name": "wiki", "kind": "ping", "target": "wiki", "site": "B"},
            "base": r["base"]})
        assert r.status_code == 409 and "changed in git" in r.json()["detail"]

        c.app.state.settings.set(configsrc.MODE, "pr")
        c.app.state.configsrc.sha = "s3"
        view = c.get("/api/config", headers=csrf).json()
        r = c.post("/api/config/entries", headers=csrf, json={
            "entry": {"name": "wiki", "kind": "ping", "target": "wiki", "site": "B"},
            "base": view["base"]}).json()
        assert r["saved"] == "pr" and r["url"].endswith("/pull/7")
        assert [w[0] for w in fake.writes[-3:]] == ["REF", "PUT", "PR"]
        assert "wiki" not in [x["name"] for x in c.get("/api/checks").json()]  # until merged


async def test_a_wrong_path_says_so_and_reading_needs_no_write_token(tmp_path, prober, caplog):
    # What happened on install day: the File box still said argus.yml.
    caplog.set_level(logging.WARNING, logger="argus")
    fake = FakeRepo()
    rt = Runtime(tmp_path / "absent.yml", tmp_path / "argus.db", prober=prober)
    poller = Poller("read", transport=httpx.MockTransport(fake))
    with TestClient(create_app(rt, start_scheduler=False, poller=poller)) as c:
        csrf = {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}
        r = c.put("/api/config/source", headers=csrf, json={
            "source": "github", "repo": "ann/ops", "path": "argus.yml"})
        assert r.status_code == 409 and "there is no argus.yml in ann/ops" in r.json()["detail"]
        assert c.get("/api/config").json()["source"] == "argus" and fake.writes == []
        view = c.put("/api/config/source", headers=csrf, json={
            "source": "github", "repo": "ann/ops", "path": "argus/argus.yml"}).json()
        assert view["source"] == "github" and "# ---- apps ----" in view["text"]


def test_checks_from_a_pasted_argus_yml_are_previewed_then_added(tmp_path):
    from argus import configsrc
    current = "checks:\n  - {name: atlas, kind: http, target: 'http://10.0.0.5:8420/'}\n"
    pasted = (
        "sites: {home: Home, chris: Chris}\n"
        "checks:\n"
        "  - {name: atlas, kind: http, target: http://10.0.0.5:8420/}\n"
        "  - {name: atlas-2, kind: http, target: http://10.0.0.5:8420}\n"
        "  - {name: shop, kind: http, target: https://shop.example/, severity: critical,"
        " site: chris}\n"
        "  - {name: Bad Name, kind: http, target: x}\n")
    items = {i["name"]: i for i in configsrc.import_preview(pasted, current)}
    assert items["atlas"]["state"] == "exists"
    assert items["atlas-2"]["state"] == "covered" and items["atlas-2"]["covered_by"] == "atlas"
    assert items["shop"]["state"] == "new" and "site" not in items["shop"]["entry"]
    assert items["Bad Name"]["state"] == "invalid"
    text = configsrc.import_checks(current, [items["shop"]["entry"]])
    assert "shop" in text and "severity: critical" in text


def test_an_import_counts_a_port_already_watched_by_another_kind():
    """couchdb-250 (HTTP /_up) and couchdb (TCP :5984) knock on one port: the
    import says it is covered, by which check, instead of adding it twice."""
    from argus import configsrc
    current = "checks:\n  - {name: couchdb, kind: tcp, target: '192.168.1.250:5984'}\n"
    pasted = ("checks:\n"
              "  - {name: couchdb-250, kind: http, target: 'http://192.168.1.250:5984/_up'}\n"
              "  - {name: other, kind: http, target: 'http://192.168.1.250:5985/'}\n")
    items = {i["name"]: i for i in configsrc.import_preview(pasted, current)}
    assert items["couchdb-250"]["state"] == "covered"
    assert items["couchdb-250"]["covered_by"] == "couchdb"
    assert items["other"]["state"] == "new"
