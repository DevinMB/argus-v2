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
