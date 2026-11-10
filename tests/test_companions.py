import base64
import json
import logging
import secrets
import textwrap

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient
from test_auth import sign_up

from argus import backup
from argus.alerts import Alerter
from argus.app import create_app
from argus.companions import CODE_TTL, DOMAIN, QUIET_AFTER, name_from
from argus.metrics import registry
from argus.runtime import Runtime
from argus.settings import Settings, load_key

CONFIG = """
checks:
  - {name: nas-web, kind: http, target: "http://127.0.0.1:5000/", companion: nas,
     severity: critical, interval: 30}
  - {name: outside, kind: tcp, target: "127.0.0.1:1"}
"""
PIN = "q83vEjRWeJq83vEjRWeJq83vEjRWeJq83vEjRWeJq8w="


@pytest.fixture
def rt(tmp_path, prober, clock):
    path = tmp_path / "argus.yml"
    path.write_text(textwrap.dedent(CONFIG))
    return Runtime(path, tmp_path / "argus.db", prober=prober, clock=clock)


@pytest.fixture
def client(rt, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    with TestClient(create_app(rt, start_scheduler=False, tls_pin=PIN)) as c:
        yield c


@pytest.fixture
def csrf(client, caplog):
    return sign_up(client, caplog)["csrf"]


def new_code(client, csrf, **body):
    r = client.post("/api/companions/codes", headers={"X-CSRF-Token": csrf},
                    json={"url": "https://argus.lan:8443", **body})
    assert r.status_code == 200, r.text
    return r.json()


def enroll(client, code, hostname="nas.lan"):
    return client.post("/companion/enroll", json={"code": code, "hostname": hostname})


def report(client, token, **fields):
    body = {"nonce": secrets.token_urlsafe(16), "hostname": "nas", **fields}
    return client.post("/companion/report", json=body,
                       headers={"Authorization": f"Bearer {token}"}), body["nonce"]


def verify(public_key, cid, nonce, r):
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
    msg = DOMAIN + cid.encode() + b"\n" + nonce.encode() + b"\n" + r.content
    key.verify(base64.b64decode(r.headers["x-argus-signature"]), msg)


def test_the_command_pins_argus_and_quotes_what_it_carries(client, csrf):
    out = new_code(client, csrf, read_only=True)
    assert out["command"].startswith(f"curl -fsS -k --pinnedpubkey sha256//{PIN} "
                                     "https://argus.lan:8443/companion/install.sh | sudo sh -s --")
    assert f"--pin {PIN}" in out["command"] and out["command"].endswith(" --read-only")
    assert out["code"] in out["command"]
    # Plain http: no pin, nothing to pin.
    plain = new_code(client, csrf, url="http://argus.lan:8080")["command"]
    assert "pinnedpubkey" not in plain and "--pin" not in plain
    odd = new_code(client, csrf, url="https://argus.lan/;reboot")["command"]
    assert "'https://argus.lan/;reboot/companion/install.sh'" in odd


def test_adding_a_host_needs_the_admin(client):
    r = client.post("/api/companions/codes", json={"url": "https://argus.lan"})
    assert r.status_code == 401


def test_a_code_enrolls_once_then_reports_are_signed(client, csrf, rt, clock):
    code = new_code(client, csrf)["code"]
    r = enroll(client, code)
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["name"] == "nas" and me["token"].startswith("argc_")
    assert enroll(client, code).status_code == 403  # single use
    # Argus keeps only a hash of the token.
    row = rt.store.db.execute("SELECT * FROM companions").fetchone()
    assert me["token"] not in json.dumps(dict(row))

    r, nonce = report(client, me["token"], os="Debian GNU/Linux 13 (trixie)",
                      version="2.0.0-dev+" + "e81b905642f66c3efe2a25a2f11a61b91ea27000",
                      disks=[{"mount": "/", "total": 100, "used": 40}],
                      containers=[{"name": "web", "state": "running", "health": "healthy"}],
                      checks=[{"name": "nas-web", "ok": False, "duration": 0.2,
                               "error": "HTTP 502"},
                              {"name": "outside", "ok": False, "duration": 0.1}])
    assert r.status_code == 200, r.text
    verify(me["public_key"], me["id"], nonce, r)
    with pytest.raises(InvalidSignature):  # bound to this host and this nonce
        verify(me["public_key"], me["id"], "another-nonce-value", r)
    reply = r.json()
    assert reply["read_only"] is False and reply["report_every"] == 60
    assert reply["checks"] == [{"name": "nas-web", "kind": "http",
                                "target": "http://127.0.0.1:5000/", "interval": 30,
                                "timeout": 5.0, "expect_status": [200], "verify_tls": True}]
    # Only the check assigned to this companion took the result.
    assert rt.store.get("nas-web").consecutive_failures == 1
    assert rt.store.get("outside").last_run is None

    view = client.get("/api/companions").json()
    (nas,) = view["companions"]
    assert nas["status"] == "ok" and nas["report"]["os"].startswith("Debian")
    assert nas["report"]["containers"][0]["health"] == "healthy"
    assert nas["checks"][0]["name"] == "nas-web" and view["unknown"] == []


def test_codes_expire_and_names_stay_unique(client, csrf, clock):
    code = new_code(client, csrf)["code"]
    clock.now += CODE_TTL + 1
    assert "expired" in enroll(client, code).json()["detail"]
    a = enroll(client, new_code(client, csrf)["code"]).json()
    b = enroll(client, new_code(client, csrf)["code"]).json()
    c = enroll(client, new_code(client, csrf, name="vault")["code"]).json()
    assert (a["name"], b["name"], c["name"]) == ("nas", "nas-2", "vault")


def test_one_command_for_several_hosts(client, csrf, clock):
    # Twelve guests to add: one command, run on each, each named after itself.
    out = new_code(client, csrf, hosts=3)
    assert out["expires"] - clock.now == 3600
    names = [enroll(client, out["code"], h).json()["name"] for h in ("shop", "notes", "nas")]
    assert names == ["shop", "notes", "nas"]
    assert "used" in enroll(client, out["code"], "fourth").json()["detail"]  # three, no more
    r = client.post("/api/companions/codes", headers={"X-CSRF-Token": csrf},
                    json={"url": "https://argus.lan", "hosts": 2, "name": "vault"})
    assert r.status_code == 422 and "names each after itself" in r.json()["detail"]
    late = new_code(client, csrf, hosts=5)["code"]
    clock.now += 3601
    assert "expired" in enroll(client, late, "late").json()["detail"]
def test_replies_name_the_companion_build_to_update_to(client, csrf, tmp_path, monkeypatch):
    import hashlib

    import argus.app
    monkeypatch.setattr(argus.app, "COMPANION_DIR", tmp_path)
    (tmp_path / "argus-companion-linux-amd64").write_bytes(b"build 1")
    me = enroll(client, new_code(client, csrf)["code"]).json()
    ro = enroll(client, new_code(client, csrf, read_only=True)["code"], "ro").json()
    reply = report(client, me["token"], arch="amd64")[0].json()
    assert reply["companion"] == {"sha256": hashlib.sha256(b"build 1").hexdigest()}
    assert "companion" not in report(client, me["token"], arch="riscv64")[0].json()
    assert "companion" not in report(client, ro["token"], arch="amd64")[0].json()


def test_removing_a_host_revokes_its_token(client, csrf):
    me = enroll(client, new_code(client, csrf)["code"]).json()
    r = client.delete(f"/api/companions/{me['id']}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert report(client, me["token"])[0].status_code == 401
    assert report(client, "argc_guess")[0].status_code == 401


def test_a_read_only_install_is_told_so(client, csrf):
    me = enroll(client, new_code(client, csrf, read_only=True)["code"]).json()
    assert report(client, me["token"])[0].json()["read_only"] is True


def test_quiet_hosts_and_full_disks_page(client, csrf, rt, clock, tmp_path):
    me = enroll(client, new_code(client, csrf)["code"]).json()
    report(client, me["token"], disks=[{"mount": "/", "total": 1000, "used": 960},
                                       {"mount": "/srv", "total": 1000, "used": 500}])
    settings = Settings(rt.store.db, load_key(tmp_path / "argus.key"))
    alerter = Alerter(rt, settings, httpx.AsyncClient())
    assert {k: p.title for k, p in alerter.problems().items()} == {
        "disk:nas:/": "/ on nas is almost full"}
    clock.now += QUIET_AFTER + 1
    assert list(alerter.problems()) == ["companion:nas"]
    reg = registry(rt)
    assert reg.get_sample_value("argus_companion_disk_used_ratio",
                                {"companion": "nas", "mount": "/"}) == 0.96
    assert reg.get_sample_value("argus_companion_last_report_timestamp_seconds",
                                {"companion": "nas"}) is not None


def test_checks_for_a_companion_are_not_run_by_argus(rt):
    rt.tick()
    assert "nas-web" not in rt.next_due and "outside" in rt.next_due


def test_install_script_and_missing_builds(client):
    r = client.get("/companion/install.sh")
    assert r.status_code == 200 and r.text.startswith("#!/bin/sh")
    assert client.get("/companion/download/linux-amd64").status_code == 404
    assert client.get("/companion/download/../../etc/passwd").status_code == 404


def test_host_names_become_companion_names():
    assert name_from("NAS01.home.lan") == "nas01"
    assert name_from("my_box") == "my-box"
    assert name_from("___") == "host"


def test_a_container_the_owner_does_not_care_about_can_be_ignored(client, csrf, rt, tmp_path):
    me = enroll(client, new_code(client, csrf)["code"]).json()
    url = f"/api/companions/{me['id']}/ignored"
    h = {"X-CSRF-Token": csrf}
    assert client.put(url, headers=h, json={"container": "smoke", "ignored": True}).json() == {
        "ignored": ["smoke"]}
    assert client.get("/api/companions").json()["companions"][0]["ignored"] == ["smoke"]
    assert client.put("/api/companions/nope/ignored", headers=h,
                      json={"container": "x", "ignored": True}).status_code == 404
    client.put(url, headers=h, json={"container": "old", "ignored": True})
    assert client.put(url, headers=h, json={"container": "old", "ignored": False}).json() == {
        "ignored": ["smoke"]}
    # It stays through a rename and an export.
    assert client.put(f"/api/companions/{me['id']}/name", headers=h,
                      json={"name": "vm"}).status_code == 200
    settings = Settings(rt.store.db, load_key(tmp_path / "argus.key"))
    blob = backup.export(settings, tmp_path, "a long passphrase")
    rt.store.db.execute("UPDATE companions SET ignored = '[]'")
    backup.restore(settings, tmp_path, blob, "a long passphrase")
    assert client.get("/api/companions").json()["companions"][0]["ignored"] == ["smoke"]


def test_add_argus_to_a_proxmox_guest_from_its_server(client, csrf, clock):
    # Design decision 14: the server's companion installs the companion in a guest.
    h = {"X-CSRF-Token": csrf}
    pve = enroll(client, new_code(client, csrf)["code"], "pve").json()
    px = {"node": "pve", "guests": [
        {"vmid": 104, "name": "couchdb", "type": "qemu", "status": "running"},
        {"vmid": 105, "name": "homepage", "type": "lxc", "status": "running"},
        {"vmid": 109, "name": "herdr-lab", "type": "qemu", "status": "stopped"}]}
    report(client, pve["token"], proxmox=px)
    base = f"/api/companions/{pve['id']}/guests"
    url = {"url": "https://192.168.10.18:8443"}
    assert client.post(f"{base}/109/install", headers=h, json=url).status_code == 409
    assert client.post(f"{base}/999/install", headers=h, json=url).status_code == 404
    assert client.post(f"{base}/105/install", headers=h,
                       json={"url": "https://x;reboot"}).status_code == 422
    r = client.post(f"{base}/105/install", headers=h, json=url)
    assert r.json() == {"ok": True, "name": "homepage"}

    # The next signed reply names the guest; the command is the companion's to build.
    reply = report(client, pve["token"], proxmox=px)[0].json()
    [todo] = reply["guest_installs"]
    assert {k: todo[k] for k in ("vmid", "type", "url")} == {
        "vmid": 105, "type": "lxc", "url": "https://192.168.10.18:8443"}
    assert set(todo) == {"vmid", "type", "url", "pin", "code", "reinstall"}
    host = client.get("/api/companions").json()["companions"][0]
    assert host["guest_installs"]["105"]["state"] == "running"

    # The guest enrolls with that code under its Proxmox name, whatever its hostname.
    guest = enroll(client, todo["code"], "localhost").json()
    assert guest["name"] == "homepage"
    done = {"vmid": 105, "code": todo["code"], "ok": True, "output": "enrolled as homepage"}
    reply = report(client, pve["token"], proxmox=px, guest_installs=[done])[0].json()
    assert "guest_installs" not in reply
    views = {c["name"]: c for c in client.get("/api/companions").json()["companions"]}
    assert views["pve"]["guest_installs"]["105"]["state"] == "ok"
    assert client.post(f"{base}/105/install", headers=h, json=url).json()["detail"] == (
        "a host called homepage is already in Argus")

    # A server that never answers: the request fails, and says why.
    client.post(f"{base}/104/install", headers=h, json=url)
    clock.now += 61 * 60
    views = {c["name"]: c for c in client.get("/api/companions").json()["companions"]}
    assert views["pve"]["guest_installs"]["104"]["state"] == "failed"
    assert "never answered" in views["pve"]["guest_installs"]["104"]["output"]


def test_a_restarted_proxmox_companion_keeps_its_guests_until_it_reads_them(client, csrf):
    pve = enroll(client, new_code(client, csrf)["code"], "pve").json()
    px = {"node": "pve", "guests": [{"vmid": 105, "name": "homepage", "type": "lxc",
                                      "status": "running"}]}
    report(client, pve["token"], kernel="7.0.14-28-pve", proxmox=px)
    report(client, pve["token"], kernel="7.0.14-28-pve")  # just restarted: no reading yet
    host = client.get("/api/companions").json()["companions"][0]
    assert host["report"]["proxmox"]["guests"][0]["name"] == "homepage"
    report(client, pve["token"], kernel="6.8.0-generic")  # not a Proxmox server any more
    assert client.get("/api/companions").json()["companions"][0]["report"]["proxmox"] is None


def test_an_appliance_guest_says_to_skip_it(client, csrf):
    h = {"X-CSRF-Token": csrf}
    pve = enroll(client, new_code(client, csrf)["code"], "pve").json()
    px = {"node": "pve", "guests": [{"vmid": 101, "name": "haos15.0", "type": "qemu",
                                      "status": "running"}]}
    report(client, pve["token"], proxmox=px)
    client.post(f"/api/companions/{pve['id']}/guests/101/install", headers=h,
                json={"url": "https://192.168.10.18:8443"})
    [todo] = report(client, pve["token"], proxmox=px)[0].json()["guest_installs"]
    report(client, pve["token"], proxmox=px, guest_installs=[
        {"vmid": 101, "code": todo["code"], "ok": False, "output": "sh: install: not found"}])
    host = client.get("/api/companions").json()["companions"][0]
    assert "Home Assistant OS" in host["guest_installs"]["101"]["output"]
    # Skip is kept with the server, beside its ignored containers.
    r = client.put(f"/api/companions/{pve['id']}/ignored", headers=h,
                   json={"container": "guest:101", "ignored": True})
    assert r.json() == {"ignored": ["guest:101"]}


def test_update_argus_goes_to_the_companion_on_its_machine(client, csrf):
    h = {"X-CSRF-Token": csrf}
    me = enroll(client, new_code(client, csrf)["code"], "argus").json()
    here = [{"name": "argus-argus-1", "image": "ghcr.io/devinmb/argus:2", "state": "running",
             "health": "healthy", "status": "", "project": "argus", "working_dir": "/opt/argus"}]
    assert client.post("/api/argus/update", headers=h).status_code == 409  # no report yet
    report(client, me["token"], containers=here)
    assert client.get("/api/argus/version").json()["host"] == "argus"
    assert client.post("/api/argus/update", headers=h).json()["update"]["state"] == "waiting"
    assert client.post("/api/argus/update", headers=h).status_code == 409  # one at a time
    ask = report(client, me["token"], containers=here)[0].json()["argus_update"]
    assert ask["image"] == "ghcr.io/devinmb/argus:2" and ask["project"] == "argus"
    # Argus restarts; the companion tells the new one how it went.
    done = {"id": ask["id"], "state": "rolled_back", "summary": "the old one is back"}
    reply = report(client, me["token"], containers=here, argus_update=done)[0].json()
    assert "argus_update" not in reply
    upd = client.get("/api/argus/version").json()["update"]
    assert (upd["state"], upd["summary"]) == ("rolled_back", "the old one is back")


def test_a_companion_stuck_on_an_old_build_is_named_with_the_command_that_fixes_it(
        client, csrf, clock, monkeypatch):
    monkeypatch.setenv("ARGUS_COMMIT", "fd5c378113c9aaaa")
    me = enroll(client, new_code(client, csrf)["code"], "argus").json()
    report(client, me["token"], version="2.0.0-dev+5bfe622edb18")
    body = client.get("/api/companions").json()
    assert body["companions"][0]["companion_stale"] is False  # it may be updating now
    assert "--code" not in body["update_command"] and "install.sh" in body["update_command"]
    clock.now += 3 * 3600 + 1
    report(client, me["token"], version="2.0.0-dev+5bfe622edb18")
    assert client.get("/api/companions").json()["companions"][0]["companion_stale"] is True
    report(client, me["token"], version="2.0.0-dev+fd5c378113c9")
    assert client.get("/api/companions").json()["companions"][0]["companion_stale"] is False


def test_reinstall_companion_goes_through_the_guests_proxmox_server(client, csrf, monkeypatch):
    monkeypatch.setenv("ARGUS_COMMIT", "12d47a634a62aaaa")
    h = {"X-CSRF-Token": csrf}
    pve = enroll(client, new_code(client, csrf)["code"], "pve").json()
    guest = enroll(client, new_code(client, csrf)["code"], "homepage").json()
    lone = enroll(client, new_code(client, csrf)["code"], "rocky").json()
    px = {"node": "pve", "guests": [{"vmid": 105, "name": "homepage", "type": "lxc",
                                      "status": "running"}]}
    url = {"url": "https://192.168.10.18:8443"}
    report(client, pve["token"], proxmox=px, version="2.0.0-dev+fd5c378113c9")
    r = client.post(f"/api/companions/{guest['id']}/reinstall", headers=h, json=url)
    assert r.status_code == 409 and "updating its companion" in r.json()["detail"]
    report(client, pve["token"], proxmox=px, version="2.0.0-dev+12d47a634a62")
    assert client.post(f"/api/companions/{guest['id']}/reinstall", headers=h,
                       json=url).json() == {"by": "pve", "vmid": 105}
    [ask] = report(client, pve["token"], proxmox=px,
                   version="2.0.0-dev+12d47a634a62")[0].json()["guest_installs"]
    assert ask["reinstall"] is True and ask["vmid"] == 105
    r = client.post(f"/api/companions/{lone['id']}/reinstall", headers=h, json=url)
    assert r.status_code == 409 and "run the command" in r.json()["detail"]


def test_uninstall_asks_for_the_password_twice_then_the_companion_then_forgets(client, csrf):
    """Decision 18: the password twice; the next reply asks the companion; its
    handback report makes Argus forget the host, and not before."""
    from test_auth import PASSWORD
    me = enroll(client, new_code(client, csrf)["code"]).json()
    url, h = f"/api/companions/{me['id']}/uninstall", {"X-CSRF-Token": csrf}
    def ask(password, again):
        return client.post(url, headers=h, json={"password": password, "again": again})
    assert ask(PASSWORD, "other").status_code == 422
    assert ask("wrong", "wrong").status_code == 403
    assert "uninstall" not in report(client, me["token"])[0].json()

    r = client.post(url, headers=h, json={"password": PASSWORD, "again": PASSWORD})
    assert r.status_code == 200 and r.json()["state"] == "waiting"
    assert report(client, me["token"])[0].json()["uninstall"] is True
    host = client.get("/api/companions").json()["companions"][0]
    assert host["uninstall"]["state"] == "waiting" and not host["uninstall"]["stale"]

    # A failed handback keeps the host, with what went wrong.
    report(client, me["token"], uninstall={"state": "failed", "log": "could not move web"})
    host = client.get("/api/companions").json()["companions"][0]
    assert host["uninstall"]["state"] == "failed" and "web" in host["uninstall"]["log"]

    client.post(url, headers=h, json={"password": PASSWORD, "again": PASSWORD})
    back = "handed back web: /opt/stacks/web"
    r, _ = report(client, me["token"], uninstall={"state": "ok", "log": back})
    assert r.status_code == 200 and "uninstall" not in r.json()
    view = client.get("/api/companions").json()
    assert view["companions"] == []
    assert view["uninstalled"][0]["log"] == back
    assert report(client, me["token"])[0].status_code == 401


def test_a_monitoring_only_host_is_uninstalled_by_its_owner_not_argus(client, csrf):
    from test_auth import PASSWORD
    me = enroll(client, new_code(client, csrf, read_only=True)["code"]).json()
    r = client.post(f"/api/companions/{me['id']}/uninstall", headers={"X-CSRF-Token": csrf},
                    json={"password": PASSWORD, "again": PASSWORD})
    assert r.status_code == 409 and "argus-companion uninstall" in r.json()["detail"]


def test_pending_packages_and_why_reach_the_host_page(client, csrf):
    me = enroll(client, new_code(client, csrf)["code"]).json()
    pkg = {"name": "libssl3t64", "from": "3.5.1-1", "to": "3.5.1-1+deb13u1", "security": True,
           "source": "openssl", "advisories": ["DSA-6012-1"], "cves": ["CVE-2026-1001"]}
    r, _ = report(client, me["token"], updates={"pending": 1, "security": 1, "manager": "apt",
                                               "packages": [pkg]})
    assert r.status_code == 200, r.text
    got = client.get("/api/companions").json()["companions"][0]["report"]["updates"]["packages"]
    assert got[0]["from"] == "3.5.1-1" and got[0]["cves"] == ["CVE-2026-1001"]
def test_leave_argus_lists_the_hosts_and_asks_each_with_the_password_twice(client, csrf):
    from test_auth import PASSWORD
    a = enroll(client, new_code(client, csrf)["code"], "web").json()
    enroll(client, new_code(client, csrf, read_only=True)["code"], "nas")
    view = client.get("/api/leave").json()
    assert sorted(h["name"] for h in view["hosts"]) == ["nas", "web"]
    assert view["outside"] == []  # nothing set up outside Argus yet
    h = {"X-CSRF-Token": csrf}
    bad = client.post("/api/companions/uninstall", headers=h,
                      json={"password": PASSWORD, "again": "x"})
    assert bad.status_code == 422
    r = client.post("/api/companions/uninstall", headers=h,
                    json={"password": PASSWORD, "again": PASSWORD})
    assert r.json()["asked"] == ["web"]  # a monitoring-only host is its owner's to uninstall
    assert report(client, a["token"])[0].json()["uninstall"] is True


def test_leave_argus_names_what_lives_outside_argus(rt, client, csrf):
    s = client.app.state.settings
    s.set("github.app", {"id": 1, "slug": "argus-devin", "name": "Argus (devin)",
                         "html_url": "https://github.com/apps/argus-devin", "owner": "devin",
                         "owner_type": "User"})
    s.set("deploy.repo", "devin/stacks")
    s.set_secret("deploy.write_token", "github_pat_x")
    s.set_secret("alerts.telegram.token", "123:abc")
    keys = {o["key"]: o for o in client.get("/api/leave").json()["outside"]}
    assert set(keys) == {"github_app", "deploy_repo", "write_token", "telegram"}
    assert keys["github_app"]["url"] == "https://github.com/settings/apps/argus-devin/advanced"
    assert "dependabot.yml" in keys["deploy_repo"]["how"]


WATCH = {"bot": "argus_watchdog_bot", "bot_id": "777", "checked_at": 1.0}


def test_a_host_that_watches_argus_is_listed_and_told_what_it_needs(client, csrf):
    h = {"X-CSRF-Token": csrf}
    me = enroll(client, new_code(client, csrf)["code"], "slayerz-lab").json()
    other = enroll(client, new_code(client, csrf)["code"], "mars-b").json()
    assert client.get("/api/info").json()["alerts"]["watched"] is None
    r, _ = report(client, me["token"], watch=WATCH)
    assert r.json()["watch"] == {"test": 0}
    assert "watch" not in report(client, other["token"])[0].json()  # only to watchers
    view = client.get("/api/alerts").json()
    assert [(w["name"], w["bot"], w["same_bot"]) for w in view["watchers"]] == [
        ("slayerz-lab", "argus_watchdog_bot", False)]
    assert client.get("/api/info").json()["alerts"]["watched"] == "hosts"
    # A test: every watcher sends one on its next report.
    assert client.post("/api/alerts/watchers/test", headers=h).json()["watch_test"] == 1
    assert report(client, me["token"], watch=WATCH)[0].json()["watch"]["test"] == 1
    # The watchdog bot leaves with Argus: the Leave page names it.
    keys = {o["key"]: o for o in client.get("/api/leave").json()["outside"]}
    assert "slayerz-lab watch Argus" in keys["watchdog:argus_watchdog_bot"]["how"]


def test_a_watcher_using_argus_own_bot_does_not_count(client, csrf):
    client.app.state.settings.set_secret("alerts.telegram.token", "777:" + "a" * 30)
    me = enroll(client, new_code(client, csrf)["code"], "slayerz-lab").json()
    r, _ = report(client, me["token"], watch=WATCH)
    assert r.json()["argus_bot"] == "777"  # the public ID only, so the watch command can refuse it
    assert "a" * 30 not in r.text
    assert client.get("/api/alerts").json()["watchers"][0]["same_bot"] is True
    assert client.get("/api/info").json()["alerts"]["watched"] is None


def test_a_watcher_hears_when_argus_updates_or_cannot_alert(client, csrf):
    s = client.app.state.settings
    me = enroll(client, new_code(client, csrf)["code"], "argus").json()
    here = [{"name": "argus-argus-1", "image": "ghcr.io/devinmb/argus:2", "state": "running",
             "health": "healthy", "status": "", "project": "argus", "working_dir": "/opt/argus"}]
    report(client, me["token"], containers=here, watch=WATCH)
    client.post("/api/argus/update", headers={"X-CSRF-Token": csrf})
    said = report(client, me["token"], containers=here, watch=WATCH)[0].json()["watch"]
    at = client.get("/api/argus/version").json()["update"]["at"]
    assert said["quiet_until"] == at + 15 * 60
    s.set("alerts.delivery", {"ok_at": 1.0, "error": "could not reach Telegram (ConnectError)",
                              "error_at": 2.0})
    said = report(client, me["token"], watch=WATCH)[0].json()["watch"]
    assert said["alerts_failing"] == "could not reach Telegram (ConnectError)"
    s.set("alerts.delivery", {"ok_at": 3.0})
    assert "alerts_failing" not in report(client, me["token"], watch=WATCH)[0].json()["watch"]


def test_something_else_can_watch_argus(client, csrf):
    h = {"X-CSRF-Token": csrf}
    r = client.put("/api/alerts", headers=h, json={"watch_elsewhere": "Prometheus at site A"})
    assert r.json()["watch_elsewhere"] == "Prometheus at site A"
    assert client.get("/api/info").json()["alerts"]["watched"] == "elsewhere"
    client.put("/api/alerts", headers=h, json={"watch_elsewhere": ""})
    assert client.get("/api/info").json()["alerts"]["watched"] is None
