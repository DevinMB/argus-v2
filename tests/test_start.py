import logging
import secrets

import pytest
from fastapi.testclient import TestClient
from test_auth import sign_up

from argus.app import create_app
from argus.runtime import Runtime


@pytest.fixture
def client(tmp_path, prober, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    rt = Runtime(tmp_path / "absent.yml", tmp_path / "argus.db", prober=prober)
    with TestClient(create_app(rt, start_scheduler=False)) as c:
        yield c, {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}


def host(c, csrf, name, containers, address_override=None):
    code = c.post("/api/companions/codes", headers=csrf,
                  json={"url": "https://argus.lan", "name": name}).json()["code"]
    tok = c.post("/companion/enroll", json={"code": code, "hostname": name}).json()["token"]
    c.post("/companion/report", headers={"Authorization": f"Bearer {tok}"},
           json={"nonce": secrets.token_urlsafe(16), "containers": containers})
    if address_override:
        c.app.state.rt.store.db.execute("UPDATE companions SET address = ? WHERE name = ?",
                                        (address_override, name))


def test_a_new_install_starts_with_nothing_done(client):
    c, _ = client
    s = c.get("/api/start").json()
    assert [x["key"] for x in s["steps"]] == [
        "alerts", "host", "watcher", "checks", "patching", "github", "deploys"]
    assert s["done"] == 0 and not s["hidden"] and s["suggestions"] == []


def test_a_hosts_published_ports_become_ready_checks(client):
    c, csrf = client
    host(c, csrf, "nas", [
        {"name": "jellyfin", "state": "running", "ports": [8096], "project": "media"},
        {"name": "stopped", "state": "exited", "ports": [9000]},
        {"name": "db", "state": "running", "ports": []},
    ], address_override="192.168.10.40")
    s = c.get("/api/start").json()
    assert s["steps"][1]["done"] and s["steps"][3]["suggest"] == 1
    (j,) = s["suggestions"]
    assert j["check"] == {"name": "jellyfin", "kind": "tcp", "target": "192.168.10.40:8096",
                          "group": "media"}
    # Adding it covers it: the suggestion goes, coverage counts it.
    base = c.get("/api/config", headers=csrf).json()["base"]
    r = c.post("/api/config/entries", headers=csrf,
               json={"entry": j["check"], "base": base})
    assert r.status_code == 200, r.text
    s = c.get("/api/start").json()
    assert s["suggestions"] == [] and s["coverage"]["watched"] == 1
    assert s["steps"][3]["done"]


def test_a_host_argus_shares_gets_checks_run_inside_it(client):
    c, csrf = client
    # The TestClient reports from "testclient", which is no IP: as if on Docker's bridge.
    host(c, csrf, "box", [{"name": "web", "state": "running", "ports": [80]}],
         address_override="172.17.0.1")
    (w,) = c.get("/api/start").json()["suggestions"]
    assert w["check"]["target"] == "127.0.0.1:80" and w["check"]["companion"] == "box"


def test_hiding_the_list_is_an_admin_write(client):
    c, csrf = client
    assert c.put("/api/start", json={"hidden": True}).status_code == 403  # no CSRF token
    assert c.put("/api/start", json={"hidden": True}, headers=csrf).json()["hidden"]


def test_a_step_once_done_stays_done(tmp_path):
    from types import SimpleNamespace

    from argus import start
    rt = SimpleNamespace(config=SimpleNamespace(checks=[]))
    alerts = {"sender": "argus", "tested": False}
    v = start.view(rt=rt, alerts=alerts, gh_connected=False, deploy_views=[], companions=[],
                   hidden=False, seen={"deploys"})
    steps = {s["key"]: s["done"] for s in v["steps"]}
    assert steps["deploys"] is True and steps["github"] is False


def test_a_port_published_on_loopback_only_is_watched_from_the_host():
    from argus.start import suggestions
    hosts = [{"name": "lab", "address": "192.168.10.47", "status": "ok", "report": {"containers": [
        {"name": "db", "image": "couchdb", "state": "running", "ports": [5984, 8080],
         "local_ports": [5984]}]}}]
    by = {s["port"]: s["check"] for s in suggestions(hosts, [])}
    assert by[8080]["target"] == "192.168.10.47:8080" and "companion" not in by[8080]
    assert by[5984] == {**by[5984], "target": "127.0.0.1:5984", "companion": "lab"}


def test_knowing_argus_stopped_is_a_setup_step(client):
    c, csrf = client
    steps = {x["key"]: x for x in c.get("/api/start").json()["steps"]}
    assert not steps["watcher"]["done"] and steps["watcher"]["href"] == "#/alerts"
    c.put("/api/alerts", headers=csrf, json={"watch_elsewhere": "Prometheus"})
    steps = {x["key"]: x for x in c.get("/api/start").json()["steps"]}
    assert steps["watcher"]["done"]
