import base64
import hashlib
import json
import logging
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient
from test_auth import sign_up

from argus.adopt import inline_secrets, parse_env
from argus.app import create_app
from argus.deploys import WRITE_TOKEN
from argus.github import Poller
from argus.runtime import Runtime

WRITE = "github_pat_" + "W" * 30
COMPOSE = "services:\n  app:\n    image: jellyfin/jellyfin\n    env_file: .env\n"


class FakeRepo:
    """ann/stacks: files by path, one branch, commits that fast-forward."""

    def __init__(self):
        self.files = {"hosts/nas": "# what nas runs\nsites/nas/old\n",
                      "sites/nas/old/compose.yml": "services: {old: {image: busybox}}\n"}
        self.head, self.commits, self.empty = "a" * 40, [], False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        base = "/repos/ann/stacks"
        if m != "GET":
            assert request.headers["authorization"] == f"Bearer {WRITE}"
            body = json.loads(request.content)
            if path.endswith("/contents/README.md") and self.empty:
                self.files["README.md"] = base64.b64decode(body["content"]).decode()
                self.head, self.empty = "c" * 40, False
                self.commits.append((body["message"], {"README.md": self.files["README.md"]}))
                return httpx.Response(201, json={"commit": {"sha": self.head}})
            if path.endswith("/git/trees"):
                self.pending = {e["path"]: e.get("content") for e in body["tree"]}
                return httpx.Response(201, json={"sha": "t"})
            if path.endswith("/git/commits"):
                self.commits.append((body["message"], dict(self.pending)))
                return httpx.Response(201, json={"sha": "b" * 40})
            if path.endswith("/git/refs/heads/main"):
                self.files.update({p: t for p, t in self.pending.items() if t is not None})
                for p in [p for p, t in self.pending.items() if t is None]:
                    self.files.pop(p, None)
                self.head = "b" * 40
                return httpx.Response(200, json={})
        blob = lambda p: hashlib.sha256(self.files[p].encode()).hexdigest()  # noqa: E731
        if path == base:
            return httpx.Response(200, json={"default_branch": "main"})
        if self.empty and path in (f"{base}/commits/main", f"{base}/commits",
                                   f"{base}/git/ref/heads/main"):
            return httpx.Response(409, json={"message": "Git Repository is empty."})
        if path == f"{base}/commits/main" or path == f"{base}/git/ref/heads/main":
            return httpx.Response(200, json={"sha": self.head, "object": {"sha": self.head}})
        if path == f"{base}/git/trees/{self.head}":
            return httpx.Response(200, json={"tree": [
                {"path": p, "type": "blob", "sha": blob(p), "size": len(t)}
                for p, t in self.files.items()]})
        if path.startswith(f"{base}/contents/") and m == "GET":
            p = path[len(f"{base}/contents/"):]
            return (httpx.Response(200, json={"path": p}) if p in self.files
                    else httpx.Response(404, json={}))
        if path.startswith(f"{base}/git/blobs/"):
            sha = path.rsplit("/", 1)[1]
            text = next(t for p, t in self.files.items() if blob(p) == sha)
            return httpx.Response(200, json={"content": base64.b64encode(text.encode()).decode()})
        if path == f"{base}/git/commits/{self.head}":
            return httpx.Response(200, json={"tree": {"sha": "t0"}})
        return httpx.Response(404, json={})


@pytest.fixture
def env(tmp_path, prober, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    fake = FakeRepo()
    rt = Runtime(tmp_path / "absent.yml", tmp_path / "argus.db", prober=prober)
    poller = Poller("read", transport=httpx.MockTransport(fake))
    with TestClient(create_app(rt, start_scheduler=False, poller=poller)) as c:
        csrf = {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}
        s = c.app.state.settings
        s.set("deploy.repo", "ann/stacks")
        s.set_secret(WRITE_TOKEN, WRITE)
        c.portal.call(c.app.state.deploys.refresh)
        code = c.post("/api/companions/codes", headers=csrf,
                      json={"url": "https://argus.lan", "name": "nas"}).json()["code"]
        tok = c.post("/companion/enroll", json={"code": code, "hostname": "nas"}).json()["token"]
        yield c, csrf, tok, fake


def report(c, tok, **fields):
    base = {"nonce": secrets.token_urlsafe(16), "policy_roots": ["/srv", "/opt"],
            "containers": [{"name": "jellyfin", "state": "running", "project": "media",
                            "working_dir": "/srv/media",
                            "config_files": ["/srv/media/compose.yml"]}]}
    return c.post("/companion/report", headers={"Authorization": f"Bearer {tok}"},
                  json={**base, **fields}).json()


def collected(text=COMPOSE, env="# media\nDB_PASSWORD='hunter2 x'\nexport TZ=UTC\n"):
    return [{"project": "media", "dir": "/srv/media", "env": env,
             "files": {"compose.yml": base64.b64encode(text.encode()).decode()}}]


def test_adopting_in_place_commits_files_and_seals_secrets(env):
    c, csrf, tok, fake = env
    report(c, tok)
    (host,) = c.get("/api/companions").json()["companions"]
    assert host["adoptable"] == [{"project": "media", "dir": "/srv/media",
                                  "containers": ["jellyfin"], "link": None}]
    assert c.post("/api/adopt/nas/media/read", headers=csrf).json()["state"] == "collecting"
    assert report(c, tok)["collect"] == ["media"]  # Argus names the project, nothing more
    assert "collect" not in report(c, tok, collected=collected())

    pv = c.get("/api/adopt/nas/media", headers=csrf).json()
    assert pv["state"] == "ready" and pv["blockers"] == []
    assert pv["path"] == "sites/nas/media" and pv["dir"] == "/srv/media"
    assert pv["secrets"] == ["DB_PASSWORD", "TZ"] and "hunter2" not in json.dumps(pv)
    rows = c.app.state.rt.store.db.execute("SELECT report FROM companions").fetchall()
    assert "hunter2" not in str([r[0] for r in rows])  # never stored

    assert c.post("/api/adopt/nas/media", headers=csrf).status_code == 200
    msg, files = fake.commits[-1]
    assert msg.startswith("Adopt media on nas")
    assert files == {"sites/nas/media/compose.yml": COMPOSE,
                     "hosts/nas": "# what nas runs\nsites/nas/old\nsites/nas/media\n"}
    assert "hunter2" not in json.dumps(fake.commits)

    stacks = {s["path"]: s for s in c.app.state.deploys.stacks_for("nas")}
    media = stacks["sites/nas/media"]
    assert media["dir"] == "/srv/media" and "DB_PASSWORD='hunter2 x'" in media["env"]
    assert "dir" not in stacks["sites/nas/old"]
    (host,) = c.get("/api/companions").json()["companions"]
    assert host["adoptable"] == []  # git deploys it now


def test_what_blocks_an_adoption_says_how_to_fix_it(env):
    c, csrf, tok, fake = env
    report(c, tok)
    c.post("/api/adopt/nas/media/read", headers=csrf)
    inline = COMPOSE + "    environment:\n      DB_PASSWORD: hunter2\n      API_KEY: ${API_KEY}\n"
    report(c, tok, policy_roots=["/opt"], collected=collected(text=inline))
    pv = c.get("/api/adopt/nas/media", headers=csrf).json()
    texts = " | ".join(b["text"] for b in pv["blockers"])
    assert "writes DB_PASSWORD into itself" in texts and "API_KEY" not in texts
    assert "outside the folders this host lets Argus manage (/opt)" in texts
    r = c.post("/api/adopt/nas/media", headers=csrf)
    assert r.status_code == 409 and fake.commits == []


def test_monitoring_only_hosts_and_strangers_get_nothing(env):
    c, csrf, tok, fake = env
    assert c.post("/api/adopt/nas/nothere/read", headers=csrf).status_code == 404
    assert c.post("/api/adopt/nas/media/read").status_code == 403  # no CSRF token


def test_env_and_inline_secret_parsing():
    env, bad = parse_env("A=1\n# c\nexport B='two words'\nC=\"x=y\"\nnot a line\n")
    assert env == {"A": "1", "B": "two words", "C": "x=y"} and len(bad) == 1
    assert inline_secrets("x:\n  DB_PASSWORD: s3cret\n  TOKEN: ${TOKEN}\n"
                          "  - GH_TOKEN=abc\n") == ["DB_PASSWORD", "GH_TOKEN"]


def test_a_stack_already_running_from_its_old_folder_waits_to_be_linked(env):
    """Migrating from stack-sync: hosts/nas lists sites/nas/old, and it runs from
    the old checkout. Deploying it elsewhere would start a second copy, so it is
    held back until linked; linking needs no commit and no write token."""
    c, csrf, tok, fake = env
    c.app.state.settings.set_secret(WRITE_TOKEN, None)
    running = [{"name": "old-old-1", "state": "running", "project": "old",
                "working_dir": "/opt/homelab-stacks/sites/nas/old",
                "config_files": ["/opt/homelab-stacks/sites/nas/old/compose.yml"]}]
    reply = report(c, tok, containers=running)
    assert reply["deploy"]["stacks"] == []  # held back, not sent
    (host,) = c.get("/api/companions").json()["companions"]
    assert host["stacks"][0]["status"] == "link"
    assert host["stacks"][0]["held"] == "/opt/homelab-stacks/sites/nas/old"
    assert host["adoptable"][0]["link"] == "sites/nas/old"

    c.post("/api/adopt/nas/old/read", headers=csrf)
    report(c, tok, containers=running, collected=[{
        "project": "old", "dir": "/opt/homelab-stacks/sites/nas/old", "env": "PASS=x\n",
        "files": {"compose.yml": base64.b64encode(
            fake.files["sites/nas/old/compose.yml"].encode()).decode()}}])
    pv = c.get("/api/adopt/nas/old", headers=csrf).json()
    assert pv["mode"] == "link" and pv["path"] == "sites/nas/old" and pv["blockers"] == []
    assert pv["differs"] == [] and pv["secrets"] == ["PASS"]
    assert c.post("/api/adopt/nas/old", headers=csrf).json() == {"commit": None}
    assert fake.commits == []  # nothing written to git

    (st,) = report(c, tok, containers=running)["deploy"]["stacks"]
    assert st["dir"] == "/opt/homelab-stacks/sites/nas/old" and st["name"] == "old"
    assert "PASS='x'" in st["env"]


def test_project_names_follow_compose():
    from argus.deploys import project_name
    assert project_name("sites/home/My_App", {"compose.yml": b"services: {}"}) == "my_app"
    assert project_name("sites/home/x", {"compose.yml": b"name: Shop\nservices: {}"}) == "shop"
    assert project_name("sites/home/x", {"docker-compose.yml": b"name: [bad]"}) == "x"


def test_a_brand_new_empty_repo_takes_the_first_stack(env):
    # Fresh setup: the owner makes an empty repo on GitHub just for Argus.
    c, csrf, tok, fake = env
    fake.files, fake.empty = {}, True
    c.portal.call(c.app.state.deploys.refresh)
    assert c.get("/api/deploys").json()["empty"] is True
    report(c, tok)
    c.post("/api/adopt/nas/media/read", headers=csrf)
    report(c, tok, collected=collected())
    assert c.get("/api/adopt/nas/media", headers=csrf).json()["blockers"] == []
    assert c.post("/api/adopt/nas/media", headers=csrf).status_code == 200
    (first, readme), (msg, files) = fake.commits
    assert first == "Start the deploy repo" and "deployed by Argus" in readme["README.md"]
    assert files == {"sites/nas/media/compose.yml": COMPOSE, "hosts/nas": "sites/nas/media\n"}
    assert c.get("/api/deploys").json()["empty"] is False


def test_dependabot_is_one_press(env):
    c, csrf, tok, fake = env
    r = c.post("/api/deploys/dependabot", headers=csrf)
    assert r.status_code == 200
    msg, files = fake.commits[-1]
    assert msg.startswith("Keep stack images up to date")
    assert 'package-ecosystem: "docker-compose"' in files[".github/dependabot.yml"]
    # A repo that already has its own config keeps it.
    fake.files[".github/dependabot.yml"] = "version: 2\nupdates: []\n"
    n = len(fake.commits)
    r = c.post("/api/deploys/dependabot", headers=csrf).json()
    assert r == {"commit": None, "existing": True}
    assert len(fake.commits) == n



def test_a_repo_that_names_a_host_differently_is_matched_up(env):
    # An existing stacks repo from before Argus: hosts/docker-1-250 is the host
    # Argus knows as nas.
    c, csrf, tok, fake = env
    fake.files = {"hosts/docker-1-250": "sites/chris/media\n",
                  "sites/chris/media/compose.yml": COMPOSE, "hosts/gone": "sites/chris/old\n"}
    c.portal.call(c.app.state.deploys.refresh)
    report(c, tok)
    view = c.get("/api/deploys").json()
    assert view["unmatched"] == [
        {"host": "docker-1-250", "stacks": ["sites/chris/media"], "suggest": "nas", "taken": False},
        {"host": "gone", "stacks": ["sites/chris/old"], "suggest": None, "taken": False}]
    r = c.post("/api/deploys/hosts", headers=csrf, json={"old": "docker-1-250", "new": "nas"})
    assert r.status_code == 200
    msg, files = fake.commits[-1]
    assert msg.startswith("Rename hosts/docker-1-250 to hosts/nas")
    assert files == {"hosts/nas": "sites/chris/media\n", "hosts/docker-1-250": None}
    # Now nas's running media project is the repo's: one to link, not to move.
    (host,) = c.get("/api/companions").json()["companions"]
    assert host["adoptable"][0]["link"] == "sites/chris/media"
    assert c.post("/api/deploys/hosts", headers=csrf, json={"old": "gone"}).status_code == 200
    assert fake.commits[-1][1] == {"hosts/gone": None}
    assert c.post("/api/deploys/hosts", headers=csrf,
                  json={"old": "x", "new": "nobody"}).status_code == 404
