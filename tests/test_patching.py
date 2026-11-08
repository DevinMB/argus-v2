import logging
import secrets
import textwrap

import pytest
from fastapi.testclient import TestClient
from test_auth import sign_up

from argus.app import create_app
from argus.runtime import Runtime

CONFIG = """
jobs:
  - {name: nas-backup, max_age: 691200}
  - {name: nas-restore-test, max_age: 3024000}
"""
TUE_0305 = {"weekday": 1, "time": "03:05", "date": "2027-01-12"}
PLAN = {"enabled": True, "day": 1, "time": "03:00", "level": "full", "reboot": True}


@pytest.fixture
def env(tmp_path, prober, clock, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    (tmp_path / "argus.yml").write_text(textwrap.dedent(CONFIG))
    rt = Runtime(tmp_path / "argus.yml", tmp_path / "argus.db", prober=prober, clock=clock)
    with TestClient(create_app(rt, start_scheduler=False)) as c:
        yield c, {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}, rt, clock


def enroll(c, csrf, name, read_only=False):
    code = c.post("/api/companions/codes", headers=csrf, json={
        "url": "https://argus.lan", "name": name, "read_only": read_only}).json()["code"]
    return c.post("/companion/enroll", json={"code": code, "hostname": name}).json()["token"]


def report(c, tok, **fields):
    return c.post("/companion/report", headers={"Authorization": f"Bearer {tok}"},
                  json={"nonce": secrets.token_urlsafe(16), **fields}).json()


def job_ok(c, rt, clock, name):
    r = c.post(f"/job/{name}", json={"result": "ok", "started": clock.now - 60,
                                     "finished": clock.now})
    assert r.status_code == 204


def test_no_backup_no_patching(env):
    # Design decision 12: Argus patches only a host it can bring back.
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    r = c.put("/api/patching/nas", headers=csrf, json=PLAN)
    assert r.status_code == 422 and "link a backup first" in r.json()["detail"]
    assert c.post("/api/patching/nas/now", headers=csrf).status_code == 409
    assert "patch" not in report(c, nas, local=TUE_0305)
    view = c.get("/api/companions").json()["companions"][0]["patching"]
    assert view["allowed"] == "nothing until it has a backup"
    assert c.app.state.alerter.problems() == {}  # patching was never on: nothing to page


def test_the_window_grants_one_run_as_far_as_the_owner_chose(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    plan = {**PLAN, "backup": "job:nas-backup"}
    assert c.put("/api/patching/nas", headers=csrf, json=plan).status_code == 200
    job_ok(c, rt, clock, "nas-backup")
    assert "patch" not in report(c, nas, local={**TUE_0305, "time": "02:59"})
    run = report(c, nas, local=TUE_0305)["patch"]
    assert run["level"] == "full" and run["reboot"] is True  # no restore test needed
    assert "patch" not in report(c, nas, local=TUE_0305,
                                 patch={"run": run["run"], "state": "running"})
    report(c, nas, local=TUE_0305, patch={"run": run["run"], "state": "ok",
                                          "summary": "12 packages"})
    assert "patch" not in report(c, nas, local={**TUE_0305, "time": "04:00"})  # once a window
    v = c.get("/api/companions").json()["companions"][0]["patching"]
    assert v["runs"][0]["result"] == "ok" and v["safety"]["net"] == "a recent backup"
    assert not v["safety"]["tested"]  # shown and encouraged, never required
    # The owner may choose less: security updates, no reboot.
    v = c.put("/api/patching/nas", headers=csrf, json={
        **plan, "level": "security", "reboot": False, "restore": "job:nas-restore-test"}).json()
    assert v["allowed"] == "security updates, no reboots"
    job_ok(c, rt, clock, "nas-restore-test")
    assert c.get("/api/companions").json()["companions"][0]["patching"]["safety"]["tested"]
    # A ZFS root is a safety net of its own.
    zfs = enroll(c, csrf, "zfs-box")
    report(c, zfs, local={**TUE_0305, "time": "01:00"}, snapshot="zfs")
    assert c.put("/api/patching/zfs-box", headers=csrf, json=PLAN).status_code == 200
    zrun = report(c, zfs, local=TUE_0305, snapshot="zfs")["patch"]
    assert zrun["level"] == "full" and zrun["snapshot"] is True


def test_the_owner_says_how_recent_a_backup_must_be(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    c.put("/api/patching/nas", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    clock.now += 9 * 86400
    assert "patch" not in report(c, nas, local=TUE_0305)
    view = c.get("/api/companions").json()["companions"][0]["patching"]
    assert view["skipped"] == "its backup (job:nas-backup) is 9 days old, over its limit of 8"
    titles = [p.title for p in c.app.state.alerter.problems().values()]
    assert "nas was not patched" in titles
    # A monthly backup is fine for this owner: the same backup now counts.
    c.put("/api/patching/nas", headers=csrf,
          json={**PLAN, "backup": "job:nas-backup", "backup_days": 31})
    assert "patch" in report(c, nas, local=TUE_0305)


def test_one_host_at_a_time_on_any_day(env):
    c, csrf, rt, clock = env
    a, b = enroll(c, csrf, "a"), enroll(c, csrf, "b")
    job_ok(c, rt, clock, "nas-backup")
    for h in ("a", "b"):
        c.put(f"/api/patching/{h}", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    run = report(c, a, local=TUE_0305)["patch"]
    assert "patch" not in report(c, b, local=TUE_0305)  # waits its turn
    report(c, a, local=TUE_0305, patch={"run": run["run"], "state": "ok"})
    brun = report(c, b, local=TUE_0305)["patch"]
    report(c, b, local=TUE_0305, patch={"run": brun["run"], "state": "ok"})
    # Sunday is a day like any other: the owner picks.
    sun = {"weekday": 6, "time": "03:05", "date": "2027-01-17"}
    r = c.put("/api/patching/a", headers=csrf,
              json={**PLAN, "day": 6, "backup": "job:nas-backup"})
    assert r.status_code == 200
    assert "patch" in report(c, a, local=sun)


def test_patch_now_stuck_runs_and_monitoring_only_hosts(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    ro = enroll(c, csrf, "ro", read_only=True)
    assert c.post("/api/patching/ro/now", headers=csrf).status_code == 409
    assert "patch" not in report(c, ro, local=TUE_0305)
    c.put("/api/patching/nas", headers=csrf,
          json={**PLAN, "enabled": False, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    c.post("/api/patching/nas/now", headers=csrf)  # a one-off, outside any window
    run = report(c, nas, local={"weekday": 3, "time": "15:00", "date": "2027-01-14"})["patch"]
    assert run["level"] == "full"
    clock.now += 2 * 3600 + 1
    report(c, nas, local={"weekday": 3, "time": "17:01", "date": "2027-01-14"})
    view = c.get("/api/companions").json()["companions"]
    nas_view = next(v for v in view if v["name"] == "nas")["patching"]
    assert nas_view["runs"][0]["result"] == "failed"
    assert "patching nas failed" in [p.title for p in c.app.state.alerter.problems().values()]
    assert c.put("/api/patching/nas", json=PLAN).status_code == 403  # an admin write


def test_a_host_patched_by_something_else(env):
    c, csrf, rt, clock = env
    nas = enroll(c, csrf, "nas")
    r = c.put("/api/patching/nas", headers=csrf, json={**PLAN, "elsewhere": True})
    assert r.status_code == 422 and "would fight" in r.json()["detail"]
    v = c.put("/api/patching/nas", headers=csrf, json={
        **PLAN, "enabled": False, "elsewhere": True, "elsewhere_job": "job:nas-backup"}).json()
    assert v["elsewhere"]["status"] == "never"
    job_ok(c, rt, clock, "nas-backup")
    view = c.get("/api/companions").json()["companions"][0]["patching"]
    assert view["elsewhere"]["status"] == "ok" and view["elsewhere"]["at"] == clock.now
    # Argus never patches it, in its window or on request.
    assert "patch" not in report(c, nas, local=TUE_0305)
    assert c.post("/api/patching/nas/now", headers=csrf).status_code == 409
    # And the checklist counts it as patched.
    steps = {s["key"]: s for s in c.get("/api/start").json()["steps"]}
    assert steps["patching"]["done"]


def test_a_removed_hosts_plan_is_not_inherited_by_a_new_host_with_its_name(env):
    # The VMID-reuse shape: a new machine under an old name must start clean.
    c, csrf, rt, clock = env
    old = enroll(c, csrf, "lab")
    c.put("/api/patching/lab", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    run = report(c, old, local=TUE_0305)["patch"]
    report(c, old, local=TUE_0305, patch={"run": run["run"], "state": "failed",
                                          "summary": "dpkg broke"})
    c.post("/api/patching/lab/now", headers=csrf)
    cid = c.get("/api/companions").json()["companions"][0]["id"]
    assert c.delete(f"/api/companions/{cid}", headers=csrf).status_code == 200
    enroll(c, csrf, "lab")
    host = c.get("/api/companions").json()["companions"][0]
    assert host["name"] == "lab"
    assert host["patching"]["plan"]["enabled"] is False
    assert host["patching"]["plan"]["backup"] == ""
    assert host["patching"]["runs"] == []
    assert c.app.state.alerter.problems() == {}  # the old host's failed run went with it


def test_a_proxmox_guest_uses_the_backup_its_server_sees(env):
    # Nothing to wire: the companion on the Proxmox server reports each guest's
    # newest vzdump backup, and the guest's own host card offers it.
    c, csrf, rt, clock = env
    pve, shop = enroll(c, csrf, "pve1"), enroll(c, csrf, "shop")
    guests = [{"vmid": 101, "name": "shop", "type": "qemu", "status": "running",
               "backup": {"at": clock.now - 86400, "storage": "nas", "size": 1}},
              {"vmid": 102, "name": "never", "type": "lxc", "status": "stopped"}]
    report(c, pve, local=TUE_0305, proxmox={"node": "pve1", "guests": guests})
    report(c, shop, local={**TUE_0305, "time": "01:00"}, hostname="shop.lan")
    body = c.get("/api/companions").json()
    assert [b["ref"] for b in body["backups"]] == ["pve:pve1/101", "pve:pve1/102"]
    views = {v["name"]: v["patching"] for v in body["companions"]}
    assert views["shop"]["suggested_backup"] == "pve:pve1/101"
    assert views["pve1"]["suggested_backup"] == "pve:guests"  # a server's net is its guests
    v = c.put("/api/patching/shop", headers=csrf, json={**PLAN, "backup": "pve:pve1/101"}).json()
    assert v["safety"]["net"] == "a recent backup"
    assert "patch" in report(c, shop, local=TUE_0305)
    # A backup the server stops seeing as recent stops the next window.
    report(c, shop, local=TUE_0305, patch={"run": 1, "state": "ok"})
    clock.now += 8 * 86400
    report(c, pve, local=TUE_0305, proxmox={"node": "pve1", "guests": guests})
    assert "patch" not in report(c, shop, local={**TUE_0305, "date": "2027-01-19"})
    assert "9 days old" in c.get("/api/companions").json()["companions"][1]["patching"]["skipped"]


def test_renaming_a_host_keeps_its_plan_runs_and_guest_backups(env):
    # Two Proxmox servers both call themselves pve; the owner renames one.
    c, csrf, rt, clock = env
    tok = enroll(c, csrf, "lab")
    c.put("/api/patching/lab", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    run = report(c, tok, local=TUE_0305)["patch"]
    report(c, tok, local=TUE_0305, patch={"run": run["run"], "state": "ok", "summary": "done"})
    enroll(c, csrf, "web")
    rt_settings = c.app.state.patching.settings
    rt_settings.set("patch.web", {**PLAN, "backup": "pve:lab/105"})
    hosts = {h["name"]: h for h in c.get("/api/companions").json()["companions"]}

    r = c.put(f"/api/companions/{hosts['lab']['id']}/name", headers=csrf, json={"name": "web"})
    assert r.status_code == 409 and "already called web" in r.json()["detail"]
    r = c.put(f"/api/companions/{hosts['lab']['id']}/name", headers=csrf, json={"name": "Bad!"})
    assert r.status_code == 422
    r = c.put(f"/api/companions/{hosts['lab']['id']}/name", headers=csrf, json={"name": "pve-b"})
    assert r.status_code == 200

    hosts = {h["name"]: h for h in c.get("/api/companions").json()["companions"]}
    assert set(hosts) == {"pve-b", "web"}
    assert hosts["pve-b"]["patching"]["plan"]["backup"] == "job:nas-backup"
    assert [x["summary"] for x in hosts["pve-b"]["patching"]["runs"]] == ["done"]
    assert hosts["web"]["patching"]["plan"]["backup"] == "pve:pve-b/105"
    assert report(c, tok)  # the same token, nothing to change on the host
    assert c.put("/api/companions/nope/name", headers=csrf,
                 json={"name": "x"}).status_code == 404


def pve_report(clock, ages):
    """A Proxmox server's reading: guest vmid -> backup age in days (None: no backup)."""
    return {"node": "pve", "guests": [
        {"vmid": v, "name": f"g{v}", "type": "lxc", "status": "running",
         **({"backup": {"at": clock.now - d * 86400, "storage": "nas"}} if d is not None else {})}
        for v, d in ages.items()]}


def test_a_proxmox_server_is_patched_only_when_every_guest_is_backed_up(env):
    c, csrf, rt, clock = env
    pve = enroll(c, csrf, "pve")
    px = pve_report(clock, {101: 2, 102: None})
    report(c, pve, kernel="7.0-pve", proxmox=px)
    host = c.get("/api/companions").json()["companions"][0]
    assert host["patching"]["suggested_backup"] == "pve:guests"
    assert c.put("/api/patching/pve", headers=csrf,
                 json={**PLAN, "backup": "pve:guests"}).status_code == 200
    assert "patch" not in report(c, pve, kernel="7.0-pve", proxmox=px, local=TUE_0305)
    view = c.get("/api/companions").json()["companions"][0]["patching"]
    assert view["safety"]["net"] == "1 guest without a recent backup: g102"
    assert view["skipped"].startswith("1 guest without a recent backup")
    # Ollama-style: excluded from backups on purpose, and the owner says so.
    cid = c.get("/api/companions").json()["companions"][0]["id"]
    c.put(f"/api/companions/{cid}/ignored", headers=csrf, json={"container": "nobackup:102",
                                                                 "ignored": True})
    assert report(c, pve, kernel="7.0-pve", proxmox=px, local=TUE_0305)["patch"]["run"]


def test_one_proxmox_server_waits_while_the_others_last_run_failed(env):
    c, csrf, rt, clock = env
    a, b = enroll(c, csrf, "pve-a"), enroll(c, csrf, "pve-b")
    px = pve_report(clock, {101: 1})
    for tok, name in ((a, "pve-a"), (b, "pve-b")):
        report(c, tok, proxmox=px)
        c.put(f"/api/patching/{name}", headers=csrf, json={**PLAN, "backup": "pve:guests"})
    run = report(c, a, proxmox=px, local=TUE_0305)["patch"]
    report(c, a, proxmox=px, local=TUE_0305, patch={"run": run["run"], "state": "failed",
                                                     "summary": "grub broke"})
    assert "patch" not in report(c, b, proxmox=px, local=TUE_0305)
    hosts = {h["name"]: h for h in c.get("/api/companions").json()["companions"]}
    assert "pve-a, another Proxmox server, failed" in hosts["pve-b"]["patching"]["skipped"]


def test_a_guest_that_never_got_its_turn_says_so(env):
    c, csrf, rt, clock = env
    tok = enroll(c, csrf, "lab")
    c.put("/api/patching/lab", headers=csrf, json={**PLAN, "backup": "job:nas-backup"})
    job_ok(c, rt, clock, "nas-backup")
    late = {**TUE_0305, "time": "09:30"}  # the window (03:00 + 6 h) is over
    assert "patch" not in report(c, tok, local=late)
    view = c.get("/api/companions").json()["companions"][0]["patching"]
    assert "window ended before its turn" in view["skipped"]


def test_guests_backups_do_not_cover_a_host_that_is_not_a_proxmox_server(env):
    c, csrf, rt, clock = env
    enroll(c, csrf, "lab")
    r = c.put("/api/patching/lab", headers=csrf, json={**PLAN, "backup": "pve:guests"})
    assert r.status_code == 422 and "not a Proxmox server" in r.json()["detail"]


def test_a_host_the_owner_can_rebuild_is_patched_without_a_backup(env):
    # Decision 12, amended by Devin (2026-11-07): "for things like this where I
    # don't care if there's a backup... I'd still like to avoid any vulns".
    c, csrf, rt, clock = env
    tok = enroll(c, csrf, "herdr-lab")
    assert c.post("/api/patching/herdr-lab/now", headers=csrf).status_code == 409
    r = c.put("/api/patching/herdr-lab", headers=csrf, json={**PLAN, "no_backup": True})
    assert r.status_code == 200
    assert r.json()["safety"]["net"] == "none, by your choice: it can be rebuilt"
    assert report(c, tok, local=TUE_0305)["patch"]["run"]


def test_a_guests_own_backup_counts_before_anything_is_linked(env):
    # argus (CT 100) had a fresh vzdump backup, but Patch now refused because no
    # plan was ever saved to link it.
    c, csrf, rt, clock = env
    pve, guest = enroll(c, csrf, "pve1"), enroll(c, csrf, "argus")
    px = {"node": "pve1", "guests": [{"vmid": 100, "name": "argus", "type": "lxc",
                                      "status": "running",
                                      "backup": {"at": clock.now - 4 * 3600, "storage": "nas"}}]}
    report(c, pve, proxmox=px)
    report(c, guest, local=TUE_0305)
    views = {v["name"]: v["patching"] for v in c.get("/api/companions").json()["companions"]}
    assert views["argus"]["safety"]["ok"] is True
    assert views["argus"]["safety"]["backup"] == "pve:pve1/100"
    assert c.post("/api/patching/argus/now", headers=csrf).status_code == 200


def test_a_backup_argus_cannot_see_is_the_owners_word_shown_as_such(env):
    # slayerz-lab: snapshotted on Sundays by a Proxmox Argus must not touch.
    c, csrf, rt, clock = env
    tok = enroll(c, csrf, "slayerz-lab")
    plan = {**PLAN, "outside_backup": "Sunday snapshot on Chris's Proxmox, to the UNAS"}
    r = c.put("/api/patching/slayerz-lab", headers=csrf, json=plan)
    assert r.status_code == 200
    assert r.json()["safety"]["net"] == (
        "a backup outside Argus (your word): Sunday snapshot on Chris's Proxmox, to the UNAS")
    assert report(c, tok, local=TUE_0305)["patch"]["run"]
