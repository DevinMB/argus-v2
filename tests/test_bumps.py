import base64
import json

import httpx

from argus.config import Repo
from argus.deploys import WRITE_TOKEN, Deploys, GitHubSource
from argus.github import Poller, Target, track
from argus.settings import Settings, load_key
from argus.state import Store

NOW = 1_800_000_000.0
WRITE = "github_pat_" + "W" * 30
COMPOSE = ("services:\n  web: {image: ghcr.io/ann/shop:sha-aaaaaaa}\n"
           "  api: {image: ghcr.io/ann/shop-api:sha-aaaaaaa}\n"
           "  beta: {image: ghcr.io/ann/shop-beta:sha-0000000}\n"
           "  webbeta: {image: ghcr.io/ann/shop-web-beta:sha-0000000}\n"
           "  other: {image: ghcr.io/ann/notes:sha-1111111}\n")


class FakeGitHub:
    def __init__(self):
        self.writes = []
        self.head = "c" * 40

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, auth = request.url.path, request.headers.get("authorization")
        if request.method != "GET":
            assert auth == f"Bearer {WRITE}", "writes use the deploy repo's own token"
            body = json.loads(request.content)
            self.writes.append((request.method, path, body))
            if path.endswith("/git/trees"):
                return httpx.Response(201, json={"sha": "t2"})
            if path.endswith("/git/commits"):
                return httpx.Response(201, json={"sha": "n" * 40})
            if path.endswith("/git/refs/heads/main"):
                assert body["force"] is False
                return httpx.Response(200, json={})
        base = "/repos/ann/stacks"
        routes = {
            base: {"default_branch": "main", "permissions": {"push": True}},
            f"{base}/commits/main": {"sha": self.head},
            f"{base}/git/trees/{self.head}": {"tree": [
                {"path": "hosts/nas", "type": "blob", "sha": "b1", "size": 20},
                {"path": "sites/home/shop/compose.yml", "type": "blob", "sha": "b2", "size": 200}]},
            f"{base}/git/blobs/b1": {"content": base64.b64encode(b"sites/home/shop\n").decode()},
            f"{base}/git/blobs/b2": {"content": base64.b64encode(COMPOSE.encode()).decode()},
            f"{base}/git/ref/heads/main": {"object": {"sha": self.head}},
            f"{base}/git/commits/{self.head}": {"tree": {"sha": "t1"}},
        }
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404, json={})


def setup(tmp_path, fake):
    store = Store(tmp_path / "db")
    settings = Settings(store.db, load_key(tmp_path / "key"))
    settings.set("deploy.repo", "ann/stacks")
    poller = Poller("read-token", clock=lambda: NOW, transport=httpx.MockTransport(fake))
    return Deploys(settings, poller, lambda: NOW), settings


async def test_a_green_build_bumps_its_tags_in_one_fast_forward_commit(tmp_path):
    fake = FakeGitHub()
    d, settings = setup(tmp_path, fake)
    await d.refresh()
    assert isinstance(d.source(), GitHubSource)
    assert d.placements("ann", "shop") == [
        {"path": "sites/home/shop", "shas": ["aaaaaaa"], "hosts": ["nas"]}]
    assert await d.bump([("ann", "shop", "bbbbbbbcafe")]) is None  # no write token: no bump
    settings.set_secret(WRITE_TOKEN, WRITE)
    assert await d.bump([("ann", "shop", "bbbbbbbcafe")]) == "n" * 40
    (_, _, tree), (_, _, commit), (_, _, ref) = fake.writes
    text = tree["tree"][0]["content"]
    assert tree["tree"][0]["path"] == "sites/home/shop/compose.yml"
    assert "shop:sha-bbbbbbb" in text and "shop-api:sha-bbbbbbb" in text
    assert "shop-beta:sha-0000000" in text and "notes:sha-1111111" in text  # untouched
    assert "shop-web-beta:sha-0000000" in text  # a component's beta follows its own branch too
    assert commit["parents"] == ["c" * 40]
    assert commit["message"].startswith("Deploy: shop sha-bbbbbbb")


async def test_no_bump_when_the_repo_moved_or_nothing_changes(tmp_path):
    fake = FakeGitHub()
    d, settings = setup(tmp_path, fake)
    settings.set_secret(WRITE_TOKEN, WRITE)
    await d.refresh()
    assert await d.bump([("ann", "shop", "aaaaaaa")]) is None  # already there
    fake.head = "d" * 40  # someone pushed since Argus read it
    assert await d.bump([("ann", "shop", "bbbbbbb")]) is None
    assert fake.writes == []


def test_track_from_companions():
    state = {"updates": [], "dependabot": None, "alerts": [], "url": "u", "stack": None,
             "main": {"sha": "bbbbbbb", "status": "success", "title": "Fix", "pr": None,
                      "merged_by": None, "url": "r", "started_at": NOW - 600,
                      "updated_at": NOW - 300, "trivy": "success", "failed_step": None}}
    old = [{"path": "sites/home/shop", "shas": ["aaaaaaa"], "hosts": ["nas"],
            "status": {"nas": "deployed"}}]
    steps = {s["key"]: s for s in track(state, None, {}, NOW, "ann/stacks", "", old)}
    assert steps["bumped"]["status"] == "running" and steps["deployed"]["status"] == "pending"
    steps = {s["key"]: s for s in track(state, None, {}, NOW, "ann/stacks", "", old,
                                        can_bump=False)}
    assert steps["bumped"]["status"] == "not_wired" and "write token" in steps["bumped"]["detail"]
    new = [{**old[0], "shas": ["bbbbbbb"], "status": {"nas": "pending"}}]
    steps = {s["key"]: s for s in track(state, None, {}, NOW, "ann/stacks", "", new)}
    assert steps["bumped"]["status"] == "done"
    assert steps["deployed"]["detail"] == "Deploying sha-bbbbbbb on nas"
    new[0]["status"] = {"nas": "rolled_back"}
    steps = {s["key"]: s for s in track(state, None, {}, NOW, "ann/stacks", "", new)}
    assert steps["deployed"]["status"] == "failed"
    new[0]["status"] = {"nas": "deployed"}
    steps = {s["key"]: s for s in track(state, None, {}, NOW, "ann/stacks", "", new)}
    assert steps["deployed"]["status"] == "done"
    assert steps["healthy"]["status"] == "not_wired"  # no check linked
    assert Target and Repo  # the poller's types, used by bump_green_builds
