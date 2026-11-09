import asyncio
import sqlite3

import httpx

from argus import selfupdate
from argus.settings import Settings, load_key


def test_it_knows_what_is_new_from_the_products_green_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_COMMIT", "aaaaaaa1111")

    def gh(req):
        if req.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": [{"head_sha": "bbbbbbb2222"}]})
        assert req.url.path.endswith("/compare/aaaaaaa1111...bbbbbbb2222")
        return httpx.Response(200, json={"ahead_by": 2, "commits": [
            {"commit": {"message": "Older change\n\nbody"}},
            {"commit": {"message": "Newest change"}}]})

    s = Settings(sqlite3.connect(":memory:"), load_key(tmp_path / "k"))
    su = selfupdate.SelfUpdate(s, httpx.AsyncClient(transport=httpx.MockTransport(gh)), lambda: 1.0)
    asyncio.run(su.check())
    v = su.view([])
    assert (v["latest"], v["behind"], v["up_to_date"]) == ("bbbbbbb2222", 2, False)
    assert v["changes"] == ["Newest change", "Older change"]
    assert v["host"] is None  # no companion on Argus's machine: the page shows the command


def test_a_check_github_did_not_answer_does_not_say_up_to_date(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_COMMIT", "aaaaaaa1111")
    real_sleep = asyncio.sleep
    monkeypatch.setattr(selfupdate.asyncio, "sleep", lambda s: real_sleep(0))
    calls = []

    def gh(req):
        calls.append(req.url.path)
        return httpx.Response(504)

    s = Settings(sqlite3.connect(":memory:"), load_key(tmp_path / "k"))
    su = selfupdate.SelfUpdate(s, httpx.AsyncClient(transport=httpx.MockTransport(gh)), lambda: 1.0)
    su.latest = "aaaaaaa1111"
    asyncio.run(su.check())
    v = su.view([])
    assert len(calls) == 2  # one more try
    assert v["up_to_date"] is False and "did not answer" in v["error"]


RELEASES = [
    {"tag_name": "v2.0.0", "name": "v2.0.0", "body": "* Big change by @devinmb in https://github.com/x/y/pull/9"},
    {"tag_name": "v1.1.0", "name": "v1.1.0",
     "body": "## What's Changed\n* Watchers by @devinmb in https://github.com/x/y/pull/8\n"
             "* Fix a typo by @devinmb in https://github.com/x/y/pull/7"},
    {"tag_name": "v1.0.2-rc1", "prerelease": True, "body": ""},
    {"tag_name": "v1.0.1", "name": "v1.0.1", "body": "* Older fix by @devinmb in https://github.com/x/y/pull/6"},
    {"tag_name": "v1.0.0", "name": "v1.0.0", "body": ""},
]


def test_a_released_argus_follows_releases_of_its_own_major(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_VERSION", "1.0.0")

    def gh(req):
        assert req.url.path.endswith("/releases")
        return httpx.Response(200, json=RELEASES)

    s = Settings(sqlite3.connect(":memory:"), load_key(tmp_path / "k"))
    su = selfupdate.SelfUpdate(s, httpx.AsyncClient(transport=httpx.MockTransport(gh)), lambda: 1.0)
    asyncio.run(su.check())
    v = su.view([])
    assert (v["latest"], v["behind"], v["up_to_date"], v["major"]) == ("1.1.0", 2, False, "2.0.0")
    assert v["changes"] == ["1.1.0: Watchers", "1.1.0: Fix a typo", "1.0.1: Older fix"]
    assert v["image"] == "ghcr.io/devinmb/argus:1" and v["pinned"] is False

    monkeypatch.setenv("ARGUS_VERSION", "1.1.0")
    asyncio.run(su.check())
    v = su.view([])
    assert (v["behind"], v["up_to_date"], v["changes"]) == (0, True, [])
