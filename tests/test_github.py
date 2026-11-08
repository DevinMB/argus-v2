from __future__ import annotations

import base64
import textwrap

import httpx
import pytest
from fastapi.testclient import TestClient

from argus import config as cfg
from argus.app import create_app
from argus.github import GATE, Poller, dependabot_runs, track
from argus.runtime import Runtime

NOW = 1_800_000_000.0
ISO = "2027-01-15T08:00:00Z"  # NOW, as GitHub writes it
GH = cfg.GitHub(owner="Owner")
TRIVY = "Trivy (fixable HIGH and CRITICAL fail the build)"  # app-ci's step name
REPOS = (cfg.Repo(name="shop", stack="sites/home/shop", check="web"), cfg.Repo(name="lib"))


def b64(text: str) -> dict:
    return {"content": base64.b64encode(text.encode()).decode()}


def fake_github(overrides: dict | None = None):
    """A tiny GitHub: path -> JSON (or an int status). Records every call."""
    routes = {
        "/repos/Owner/homelab-stacks/contents/hosts": [
            {"type": "file", "name": "box", "path": "hosts/box"},
        ],
        "/repos/Owner/homelab-stacks/contents/hosts/box": b64("# comment\nsites/home/shop\n"),
        "/repos/Owner/homelab-stacks/contents/sites/home/shop/compose.yml": b64(
            "image: ghcr.io/owner/shop-api:sha-abc1234\n"
            "image: ghcr.io/owner/shop-web:sha-abc1234\n"
            "image: ghcr.io/owner/shop-beta:sha-0ld0000\n"
        ),
        "/repos/Owner/homelab-stacks/commits": [
            {"commit": {"committer": {"date": "2027-01-15T07:30:00Z"}}},
        ],
        "/repos/Owner/shop/pulls?open": [
            {"number": 12, "title": "Bump react 19 to 20", "html_url": "u12", "created_at": ISO,
             "user": {"login": "dependabot[bot]"}, "head": {"sha": "p12"},
             "labels": [{"name": "waits-for-devin"}]},
            {"number": 13, "title": "Someone's feature", "html_url": "u13", "created_at": ISO,
             "user": {"login": "Owner"}, "head": {"sha": "p13"}, "labels": []},
        ],
        "/repos/Owner/shop/pulls?closed": [
            # The list endpoint has no merged_by; /pulls/<n> does.
            {"number": 10, "title": "Bump httpx", "html_url": "u10",
             "merged_at": "2027-01-15T07:00:00Z", "user": {"login": "dependabot[bot]"}},
            {"number": 3, "title": "Old", "html_url": "u3", "merged_at": "2026-01-01T00:00:00Z",
             "user": {"login": "Owner"}},
            {"number": 4, "title": "Closed unmerged", "html_url": "u4", "merged_at": None,
             "user": {"login": "dependabot[bot]"}},
        ],
        "/repos/Owner/shop/pulls/10": {"merged_by": {"login": GATE}},
        "/repos/Owner/shop/pulls/11": {"merged_by": {"login": "Owner"}},
        "/repos/Owner/shop/actions/workflows/ci.yml/runs": {"workflow_runs": [
            {"id": 99, "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
             "head_sha": "abc1234deadbeef", "html_url": "run99",
             "run_started_at": "2027-01-15T07:05:00Z", "created_at": "2027-01-15T07:05:00Z",
             "updated_at": "2027-01-15T07:15:00Z",
             "head_commit": {"message": "Merge pull request #10 from dependabot/pip/x\n\nBump"}},
        ]},
        "/repos/Owner/shop/actions/runs/99/jobs": {"jobs": [
            {"name": "ci / build", "steps": [
                {"name": "Build", "conclusion": "success"},
                {"name": TRIVY, "conclusion": "success"},
            ]},
        ]},
        "/repos/Owner/shop/actions/runs": {"workflow_runs": [
            {"status": "completed", "conclusion": "success"},
        ]},
        "/repos/Owner/shop/dependabot/alerts": [
            {"security_vulnerability": {"severity": "critical", "package": {"name": "pillow"}},
             "security_advisory": {"summary": "Heap overflow"},
             "created_at": "2027-01-10T00:00:00Z", "html_url": "a1"},
        ],
        "/repos/Owner/lib/pulls?open": [],
        "/repos/Owner/lib/pulls?closed": [],
        "/repos/Owner/lib/actions/workflows/ci.yml/runs": 404,
        "/repos/Owner/lib/dependabot/alerts": 403,
    }
    routes.update(overrides or {})
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((path, dict(request.url.params)))
        assert request.headers["Authorization"] == "Bearer t0k"
        assert request.method == "GET"
        key = f"{path}?{request.url.params['state']}" if path.endswith("/pulls") else path
        body = routes.get(key, 404)
        if isinstance(body, int):
            return httpx.Response(body, json={"message": "nope"})
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler), calls


async def polled(overrides=None, repos=REPOS) -> Poller:
    transport, _ = fake_github(overrides)
    p = Poller("t0k", clock=lambda: NOW, transport=transport)
    await p.poll(GH, repos)
    return p


async def test_poll_reads_the_pipeline():
    p = await polled()
    assert p.last_error == "" and p.last_success == NOW
    shop = p.repos["shop"]
    assert shop["main"]["sha"] == "abc1234"
    assert shop["main"]["pr"] == 10 and shop["main"]["merged_by"] == GATE
    assert shop["main"]["trivy"] == "success" and shop["main"]["failed_step"] is None
    # Only Dependabot's PRs are updates; the human one is not.
    assert [u["number"] for u in shop["updates"]] == [12]
    assert shop["updates"][0]["waits_for_you"] and shop["updates"][0]["ci"] == "success"
    # Merged in the last 14 days only, unmerged ones skipped.
    assert [(m["number"], m["by"]) for m in shop["merged"]] == [(10, GATE)]
    assert shop["alerts"][0]["severity"] == "critical"
    # The -beta image follows another branch, so it is not main's tag.
    assert shop["stack"]["shas"] == ["abc1234"] and shop["stack"]["host"] == "box"
    lib = p.repos["lib"]
    assert lib["main"] is None and lib["alerts"] is None and lib["stack"] is None


async def test_merged_by_is_fetched_once_per_pr():
    transport, calls = fake_github()
    p = Poller("t0k", clock=lambda: NOW, transport=transport)
    await p.poll(GH, REPOS)
    await p.poll(GH, REPOS)
    assert [c for c, _ in calls].count("/repos/Owner/shop/pulls/10") == 1


async def test_a_failed_poll_keeps_the_last_results():
    p = await polled()
    transport, _ = fake_github({"/repos/Owner/shop/pulls?open": 500})
    p.client = httpx.AsyncClient(base_url="https://api.github.com", transport=transport,
                                 headers={"Authorization": "Bearer t0k"})
    p.clock = lambda: NOW + 300
    await p.poll(GH, REPOS)
    assert "500" in p.last_error
    assert p.last_success == NOW and p.repos["shop"]["main"]["sha"] == "abc1234"


async def test_failed_build_names_the_step():
    p = await polled({
        "/repos/Owner/shop/actions/workflows/ci.yml/runs": {"workflow_runs": [
            {"id": 7, "event": "pull_request", "status": "completed", "conclusion": "success",
             "head_sha": "ffff000", "html_url": "pr", "created_at": ISO, "updated_at": ISO},
            {"id": 98, "event": "push", "status": "completed", "conclusion": "failure",
             "head_sha": "bad0000", "html_url": "run98", "created_at": ISO, "updated_at": ISO,
             "head_commit": {"message": "Bump pillow (#11)"}},
        ]},
        "/repos/Owner/shop/actions/runs/98/jobs": {"jobs": [
            {"name": "ci / build", "steps": [
                {"name": "Build", "conclusion": "success"},
                {"name": TRIVY, "conclusion": "failure"},
            ]},
        ]},
    })
    main = p.repos["shop"]["main"]
    # A PR run is never "main", even when it is newer.
    assert main["sha"] == "bad0000" and main["status"] == "failure" and main["pr"] == 11
    assert main["title"] == "Bump pillow" and main["merged_by"] == "Owner"
    assert main["failed_step"].startswith("ci / build: Trivy")


async def test_main_follows_a_newer_build_by_another_workflow():
    # A merge gate that dispatches the release, not CI: that commit is main's
    # newest build, so the track does not sit on CI's older one.
    release = {"id": 120, "event": "workflow_dispatch", "status": "completed",
               "conclusion": "success", "head_sha": "a520005ffff", "html_url": "run120",
               "created_at": "2027-01-15T08:00:00Z", "updated_at": "2027-01-15T08:04:00Z",
               "head_commit": {"message": "Merge pull request #11 from dependabot/npm/vite"}}
    p = await polled({
        "/repos/Owner/shop/actions/runs": {"workflow_runs": [
            {"event": "dynamic", "status": "completed", "conclusion": "success",
             "head_sha": "zzz", "created_at": "2027-01-15T08:01:00Z"},
            release]},
        "/repos/Owner/shop/actions/runs/120/jobs": {"jobs": []},
    })
    main = p.repos["shop"]["main"]
    assert main["sha"] == "a520005" and main["pr"] == 11 and main["trivy"] is None
    # An older run by another workflow does not replace CI's newer one.
    old = {**release, "created_at": "2027-01-15T07:00:00Z"}
    p = await polled({"/repos/Owner/shop/actions/runs": {"workflow_runs": [old]}})
    assert p.repos["shop"]["main"]["sha"] == "abc1234"


# ---- tracks ----

def state(**over) -> dict:
    s = {
        "name": "shop", "url": "https://github.com/Owner/shop", "check": "web", "updates": [],
        "merged": [], "alerts": [],
        "main": {"sha": "abc1234", "status": "success", "url": "run", "title": "Bump httpx",
                 "pr": 10, "author": "dependabot[bot]", "merged_by": GATE, "started_at": NOW - 900,
                 "updated_at": NOW - 600, "trivy": "success", "failed_step": None},
        "stack": {"path": "sites/home/shop", "host": "box", "shas": ["abc1234"],
                  "changed_at": NOW - 500, "url": "compose"},
    }
    s.update(over)
    return s


UP = {"name": "web", "status": "up", "last_error": ""}
FRESH = {"stack-sync-box": {"status": "ok", "last_seen": NOW - 60}}


def statuses(steps) -> dict:
    return {s["key"]: s["status"] for s in steps}


def test_track_all_the_way_to_healthy():
    steps = track(state(), UP, FRESH, NOW)
    assert [s["key"] for s in steps] == [
        "updates", "merged", "built", "bumped", "deployed", "healthy"]
    assert set(statuses(steps).values()) == {"done"}
    assert "merged by the gate" in steps[1]["detail"]
    assert steps[2]["detail"] == "sha-abc1234, Trivy clean"


def test_track_waits_for_you_on_a_major():
    up = {"number": 12, "title": "Bump react", "url": "u", "ci": "success", "waits_for_you": True}
    s = track(state(updates=[up]), UP, FRESH, NOW)
    assert s[0]["status"] == "waiting" and "waits for you" in s[0]["detail"]


def test_track_red_update_ci_outranks_waiting():
    ups = [{"number": 12, "title": "Major", "url": "u", "ci": "success", "waits_for_you": True},
           {"number": 14, "title": "Patch", "url": "v", "ci": "failure", "waits_for_you": False}]
    s = track(state(updates=ups), UP, FRESH, NOW)
    assert s[0]["status"] == "failed" and s[0]["url"] == "v"


def test_track_bump_is_running_then_failed():
    st = state(stack={**state()["stack"], "shas": ["0ld0000"]})
    assert statuses(track(st, UP, FRESH, NOW))["bumped"] == "running"
    later = track(st, UP, FRESH, NOW + 3600)
    assert statuses(later)["bumped"] == "failed"
    assert statuses(later)["deployed"] == "pending" and statuses(later)["healthy"] == "pending"


def test_track_deploy_waits_for_stack_sync():
    stale = {"stack-sync-box": {"status": "ok", "last_seen": NOW - 900}}
    assert statuses(track(state(), UP, stale, NOW))["deployed"] == "running"
    late = {"stack-sync-box": {"status": "late", "last_seen": NOW - 9000}}
    assert statuses(track(state(), UP, late, NOW))["deployed"] == "failed"
    assert statuses(track(state(), UP, {}, NOW))["deployed"] == "not_wired"


def test_a_waiting_step_says_what_it_waits_for_and_since_when():
    # Built 10 minutes ago (main's updated_at is NOW - 600), not bumped yet.
    st = state(stack={**state()["stack"], "shas": ["0ld0000"]})
    bumped = {s["key"]: s for s in track(st, UP, FRESH, NOW)}["bumped"]
    assert bumped["detail"] == ("Built 10m ago; waiting for stack-bump to put sha-abc1234 in "
                                "homelab-stacks (it runs every 5 minutes)")
    # Past 15 minutes it says so, well before the 30-minute failure.
    slow = {s["key"]: s for s in track(st, UP, FRESH, NOW + 600)}["bumped"]
    assert slow["status"] == "running"
    assert slow["detail"].startswith("Taking longer than usual: Built 20m ago;")
    # Bumped 500 s ago, stack-sync not run since.
    stale = {"stack-sync-box": {"status": "ok", "last_seen": NOW - 900}}
    deployed = {s["key"]: s for s in track(state(), UP, stale, NOW)}["deployed"]
    assert deployed["detail"].startswith("sha-abc1234 went into homelab-stacks 8m ago; "
                                         "waiting for stack-sync on box")


def test_a_paused_check_is_neither_healthy_nor_failed():
    paused = {**UP, "status": "paused", "paused": {"reason": "off for now", "since": NOW - 60}}
    healthy = track(state(), paused, FRESH, NOW)[-1]
    assert (healthy["status"], healthy["detail"]) == ("paused", "web is paused: off for now")


def test_track_build_running_and_failed():
    running = state(main={**state()["main"], "status": "running"})
    st = statuses(track(running, UP, FRESH, NOW))
    assert st["built"] == "running" and st["bumped"] == "pending"
    failed = state(main={**state()["main"], "status": "failure",
                         "failed_step": "ci / build: Tests"})
    steps = track(failed, UP, FRESH, NOW)
    assert statuses(steps)["built"] == "failed" and "Tests" in steps[2]["detail"]


def test_track_without_ci_is_not_wired_never_green():
    st = statuses(track(state(main=None), UP, FRESH, NOW))
    assert st["merged"] == "not_wired" and st["bumped"] == "pending"
    assert "done" not in (st["deployed"], st["healthy"])


def test_track_of_a_library_ends_at_the_build():
    # No stack, no deploy timer: it ships inside another app.
    steps = track(state(stack=None), None, {}, NOW)
    assert [s["key"] for s in steps] == ["updates", "merged", "built"]


def test_track_deployed_by_its_own_timer():
    # Done only when the timer reports the built commit running; its time is
    # when that was first reported, not the timer's last run.
    st = state(stack=None)
    beat = lambda version, status="ok": {"mars-sync": {  # noqa: E731
        "status": status, "last_seen": NOW - 30, "version": version, "version_at": NOW - 4000}}
    steps = track(st, UP, beat("abc1234" + "0" * 33), NOW, deploy="mars-sync")
    assert [s["key"] for s in steps] == ["updates", "merged", "built", "deployed", "healthy"]
    assert set(statuses(steps).values()) == {"done"} and steps[3]["at"] == NOW - 4000
    # Ran after the build but still on the old commit (a Flink change waits): not deployed.
    old = statuses(track(st, UP, beat("0ld0000"), NOW, deploy="mars-sync"))
    assert old["deployed"] == "running" and old["healthy"] == "pending"
    late = statuses(track(st, UP, beat("0ld0000", "late"), NOW, deploy="mars-sync"))
    assert late["deployed"] == "failed"
    assert statuses(track(st, UP, beat(None), NOW, deploy="mars-sync"))["deployed"] == "not_wired"
    assert statuses(track(st, UP, {}, NOW, deploy="mars-sync"))["deployed"] == "not_wired"


def test_track_says_when_dependabot_looked_and_what_is_open():
    dep = {"last_run": NOW - 86400, "failed": None, "url": None}
    alert = {"severity": "high", "package": "x", "summary": "s", "created_at": NOW, "url": "a"}
    s = track(state(dependabot=dep, alerts=[alert]), UP, FRESH, NOW)[0]
    assert s["status"] == "done" and s["at"] == NOW - 86400
    assert "last looked" in s["detail"] and "1 open security alert" in s["detail"]
    bad = {**dep, "failed": "pip in /backend - Update #1", "url": "https://x"}
    s = track(state(dependabot=bad), UP, FRESH, NOW)[0]
    assert s["status"] == "failed" and "pip in /backend" in s["detail"]


def test_dependabot_runs_reads_the_newest_batch():
    def run(name, at, conclusion="success", path="dynamic/dependabot/dependabot-updates"):
        return {"name": name, "created_at": at, "conclusion": conclusion, "path": path,
                "html_url": f"https://x/{name}"}
    graph = run("graph", "2026-10-26T23:00:00Z", path="dynamic/dependabot/update-graph")
    assert dependabot_runs([graph]) is None
    runs = [run("npm", "2026-10-26T23:47:12Z"), run("pip", "2026-10-26T23:40:00Z", "failure"),
            run("old", "2026-10-19T23:00:00Z", "failure")]
    d = dependabot_runs(runs)
    assert d["failed"] == "pip" and d["url"] == "https://x/pip"
    assert d["last_run"] == dependabot_runs(runs[:1])["last_run"]
    assert dependabot_runs(runs[:1])["failed"] is None


def test_track_times_are_the_events_not_the_timers():
    steps = {s["key"]: s for s in track(state(), {**UP, "last_change": NOW - 7200}, FRESH, NOW)}
    # The stack's last change (deployed within 5 minutes), not stack-sync's last run;
    # the tag bump has no time of its own, since that change may be another image's.
    assert steps["deployed"]["at"] == NOW - 500 and steps["bumped"]["at"] is None
    assert steps["healthy"]["at"] == NOW - 7200


def test_track_check_down_fails_healthy():
    down = {"name": "web", "status": "down", "last_error": "HTTP 502"}
    s = track(state(), down, FRESH, NOW)
    assert s[-1]["status"] == "failed" and s[-1]["detail"] == "web is down: HTTP 502"


# ---- config ----

def load(tmp_path, text):
    p = tmp_path / "argus.yml"
    p.write_text(textwrap.dedent(text))
    return cfg.load(p)[0]


BASE = """
checks:
  - {name: web, kind: http, target: "http://127.0.0.1:1/", site: B}
"""


def test_config_repos(tmp_path):
    c = load(tmp_path, BASE + """
github: {owner: Owner}
repos:
  - {name: shop, stack: sites/home/shop, check: web}
  - {name: lib}
""")
    assert c.github.stacks_repo == "homelab-stacks" and c.repos[0].check == "web"


@pytest.mark.parametrize("extra, msg", [
    ("repos: [{name: shop, stack: sites/home/shop}]", "with a stack need a github section"),
    ("github: {owner: O}\nrepos: [{name: shop, check: nope}]", "no check named nope"),
    ("github: {owner: O}\nrepos: [{name: a, check: web}, {name: b, check: web}]",
     "more than one repo"),
    ("github: {owner: O}\nrepos: [{name: a}, {name: a}]", "duplicate repos"),
    ("github: {owner: O}\nrepos: [{name: a, stack: ../etc}]", "stack"),
])
def test_config_repos_rejected(tmp_path, extra, msg):
    with pytest.raises(ValueError, match=msg):
        load(tmp_path, BASE + extra)


# ---- API and metrics ----

async def test_api_and_metrics(tmp_path, prober):
    p = tmp_path / "argus.yml"
    p.write_text(textwrap.dedent(BASE + """
heartbeats:
  - {name: stack-sync-box, max_age: 900, site: B}
github: {owner: Owner}
repos:
  - {name: shop, stack: sites/home/shop, check: web}
  - {name: lib}
"""))
    rt = Runtime(p, tmp_path / "db.sqlite", prober=prober, clock=lambda: NOW)
    gh = await polled(repos=rt.config.repos)
    rt.beat("stack-sync-box", "test")
    with TestClient(create_app(rt, start_scheduler=False, poller=gh)) as client:
        u = client.get("/api/updates").json()
        assert u["enabled"] and [r["name"] for r in u["repos"]] == ["shop", "lib"]
        assert u["repos"][0]["track"][0]["status"] == "waiting"
        web = client.get("/api/checks/web").json()
        assert web["repo"] == "shop" and web["track"][-2]["status"] == "done"
        m = client.get("/metrics").text
        assert 'argus_repo_open_alerts{repo="shop",severity="critical"} 1.0' in m
        assert 'argus_repo_update_prs{repo="shop",state="waiting"} 1.0' in m
        assert 'argus_repo_main_ci_ok{repo="shop"} 1.0' in m
        assert 'argus_repo_open_alerts{repo="lib"' not in m  # alerts unreadable: no series
        assert "argus_github_last_poll_success_timestamp_seconds" in m


def test_api_without_a_token(tmp_path, config_file, prober):
    rt = Runtime(config_file, tmp_path / "db.sqlite", prober=prober)
    with TestClient(create_app(rt, start_scheduler=False)) as client:
        assert client.get("/api/updates").json()["enabled"] is False
        assert client.get("/api/checks/web").json()["track"] is None
        assert "argus_repo_" not in client.get("/metrics").text


def test_track_green_update_that_never_merged_needs_a_look():
    up = {"number": 11, "title": "Bump surefire", "url": "u", "ci": "success",
          "waits_for_you": False, "created_at": NOW - 7200}
    s = track(state(updates=[up]), UP, FRESH, NOW)
    assert s[0]["status"] == "waiting" and "merge gate" in s[0]["detail"]
    fresh = {**up, "created_at": NOW - 600}
    assert track(state(updates=[fresh]), UP, FRESH, NOW)[0]["status"] == "running"


def test_one_failed_poll_is_not_shown():
    p = Poller("t0k", clock=lambda: NOW)
    p.last_success, p.last_error, p.failures = NOW, "HTTP 500", 1
    assert p.shown_error == ""
    p.failures = 3
    assert p.shown_error == "HTTP 500"
    p.last_success, p.failures = None, 1
    assert p.shown_error == "HTTP 500"


def test_a_repo_finds_its_check_by_name_when_argus_yml_does_not_link_one():
    from types import SimpleNamespace as C

    from argus.github import check_for
    checks = [C(name="sassy-routes", severity="critical"),
              C(name="sassy-routes-edge-1", severity="warning"),
              C(name="bouy-lab-frontend-1", severity="warning"),
              C(name="mars-console", severity="warning"),
              C(name="mars-jobmanager", severity="warning"),
              C(name="vault-mcp-a", severity="warning"), C(name="vault-mcp-b", severity="critical")]
    assert check_for("sassy-routes", checks) == "sassy-routes"   # named after the repo
    assert check_for("DevinMB/bouy-lab", checks) == "bouy-lab-frontend-1"  # the only one
    assert check_for("vault-mcp", checks) == "vault-mcp-b"  # several: the one that alerts
    assert check_for("mars", checks) == ""  # several, none alerts: no guess
    assert check_for("atlas", checks) == ""
