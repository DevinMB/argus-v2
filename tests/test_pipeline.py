"""The app pipeline Argus offers each repo (decision 20)."""

import base64
import json
import logging

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from test_auth import sign_up
from test_ghconnect import PAT, PEM

from argus import ghauth, pipeline
from argus.app import create_app
from argus.github import Poller
from argus.runtime import Runtime

SHARED = """name: ci
on: {pull_request: {}, push: {branches: [main]}}
jobs:
  backend:
    uses: DevinMB/workflows/.github/workflows/app-ci.yml@main
    with:
      image: ghcr.io/devinmb/bouy-lab-backend
      context: backend
      test-command: python -m pytest -q
      gate: false
  web:
    uses: DevinMB/workflows/.github/workflows/app-ci.yml@main
    with: {image: ghcr.io/devinmb/bouy-lab-frontend, context: frontend, smoke: false,
           push-ref: refs/heads/beta}
  gate:
    needs: [backend, web]
    uses: DevinMB/workflows/.github/workflows/dependabot-gate.yml@main
"""
PATHS = ["backend/Dockerfile", "backend/requirements.txt", "frontend/Dockerfile",
         "frontend/package.json", "frontend/node_modules/x/Dockerfile", "tests/Dockerfile",
         "README.md"]
TEXTS = {"backend/Dockerfile": "FROM python:3.12-slim\nHEALTHCHECK CMD curl -f localhost\n",
         "frontend/Dockerfile": "FROM nginx:alpine\n"}


def test_a_fresh_repo_gets_one_job_per_dockerfile_and_a_weekly_cache():
    p = pipeline.plan("Ann", "shop", "trunk", PATHS, TEXTS, None, False)
    assert p.kind == "none" and not p.blocked
    assert [(i.job, i.image, i.context, i.smoke) for i in p.images] == [
        ("backend", "ghcr.io/ann/shop-backend", "backend", True),
        ("frontend", "ghcr.io/ann/shop-frontend", "frontend", False)]
    ci = p.files[".github/workflows/ci.yml"]
    assert ci.startswith(pipeline.MARK + "1)")
    doc = yaml.safe_load(ci)
    assert set(doc["jobs"]) == {"backend", "frontend", "gate"}
    assert doc["jobs"]["gate"]["needs"] == ["backend", "frontend"]
    assert doc[True]["push"]["branches"] == ["trunk"]
    build = next(s for s in doc["jobs"]["backend"]["steps"] if s.get("name") == "Build")
    assert build["with"]["cache-from"] == "type=gha,scope=backend-${{ steps.week.outputs.week }}"
    names = [s.get("name") for s in doc["jobs"]["frontend"]["steps"]]
    assert "Smoke (healthcheck)" not in names and "Tests (inside the image)" not in names
    assert "Smoke (healthcheck)" in [s.get("name") for s in doc["jobs"]["backend"]["steps"]]
    # Nothing runs from another repo: every uses: is a pinned action.
    assert "app-ci.yml" not in ci
    for line in ci.splitlines():
        if "uses:" in line:
            assert "@" in line and "# v" in line, line
    dep = yaml.safe_load(p.files[".github/dependabot.yml"])
    got = {(u["package-ecosystem"], u["directory"]) for u in dep["updates"]}
    assert got == {("docker", "/backend"), ("docker", "/frontend"), ("pip", "/backend"),
                   ("npm", "/frontend"), ("github-actions", "/")}
    assert any("has no HEALTHCHECK" in n for n in p.notes)


def test_a_shared_workflow_caller_keeps_its_image_names_and_settings():
    p = pipeline.plan("DevinMB", "bouy-lab", "main", PATHS, TEXTS, SHARED, True)
    assert p.kind == "shared"
    assert [(i.job, i.image, i.context, i.test_command, i.smoke, i.branch) for i in p.images] == [
        ("backend", "ghcr.io/devinmb/bouy-lab-backend", "backend", "python -m pytest -q", True, ""),
        ("web", "ghcr.io/devinmb/bouy-lab-frontend", "frontend", "", False, "beta")]
    doc = yaml.safe_load(p.files[".github/workflows/ci.yml"])
    assert doc[True]["push"]["branches"] == ["main", "beta"]
    push = next(s for s in doc["jobs"]["web"]["steps"] if s.get("name") == "Push to GHCR")
    assert push["if"].startswith("github.ref == 'refs/heads/beta'")
    tests = next(s for s in doc["jobs"]["backend"]["steps"]
                 if s.get("name") == "Tests (inside the image)")
    assert tests["env"]["TEST_COMMAND"] == "python -m pytest -q"
    assert ".github/dependabot.yml" not in p.files  # the repo's own stays


def test_a_workflow_argus_did_not_write_is_never_replaced():
    p = pipeline.plan("ann", "shop", "main", PATHS, TEXTS, "name: ci\njobs: {}\n", False)
    assert p.kind == "own" and "does not replace" in p.blocked and not p.files
    assert pipeline.plan("ann", "x", "main", ["README.md"], {}, None, False).blocked.startswith(
        "No Dockerfile")


def test_owner_edits_and_odd_values_are_written_safely():
    edits = [{"job": "backend", "image": "ghcr.io/ann/api",
              "test_command": "pytest -q 'a b' \"$X\""}]
    p = pipeline.plan("ann", "shop", "main", PATHS, TEXTS, None, False, edits)
    doc = yaml.safe_load(p.files[".github/workflows/ci.yml"])
    job = doc["jobs"]["backend"]
    assert job["env"]["IMAGE"] == "ghcr.io/ann/api"
    tests = next(s for s in job["steps"] if s.get("name") == "Tests (inside the image)")
    assert tests["env"]["TEST_COMMAND"] == "pytest -q 'a b' \"$X\""
    assert "${{" not in tests["run"]  # values reach the shell through env only


def test_the_kind_of_a_ci_file():
    assert pipeline.kind_of(None) == "none"
    assert pipeline.kind_of(SHARED) == "shared"
    made = pipeline.plan("a", "b", "main", PATHS, TEXTS, None, True).files[
        ".github/workflows/ci.yml"]
    assert pipeline.kind_of(made) == "argus" and pipeline.version_of(made) == pipeline.VERSION


# --- the endpoints, against a small GitHub ---

class Repo:
    """One repo on a fake GitHub: its files, refs, pull requests."""

    def __init__(self, files):
        self.files = dict(files)
        self.branch_files = None
        self.prs = []
        self.write_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        auth = request.headers.get("authorization", "")
        if path == "/user/repos":
            return httpx.Response(200, json=[{
                "full_name": "ann/shop", "name": "shop", "owner": {"login": "ann"},
                "default_branch": "main", "private": True,
                "html_url": "https://github.com/ann/shop"}])
        if auth != f"Bearer {PAT}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        base = "/repos/ann/shop"
        if path == f"{base}/git/trees/main":
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob"}
                                                      for p in self.files]})
        if path.startswith(f"{base}/contents/") and m == "GET":
            p = path.removeprefix(f"{base}/contents/")
            files = self.branch_files if request.url.params.get("ref") == pipeline.BRANCH \
                else self.files
            if files is None or p not in files:
                return httpx.Response(404, json={"message": "Not Found"})
            if "raw" in request.headers["accept"]:
                return httpx.Response(200, text=files[p])
            return httpx.Response(200, json={"sha": "s-" + p})
        if path == f"{base}/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "main-sha"}})
        if not self.write_ok:
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if path == f"{base}/git/refs" and m == "POST":
            if self.branch_files is not None:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.branch_files = dict(self.files)
            return httpx.Response(201, json={})
        if path == f"{base}/git/refs/heads/{pipeline.BRANCH}" and m == "PATCH":
            assert json.loads(request.content)["force"] is True
            self.branch_files = dict(self.files)
            return httpx.Response(200, json={})
        if path.startswith(f"{base}/contents/") and m == "PUT":
            body = json.loads(request.content)
            assert body["branch"] == pipeline.BRANCH  # never the default branch
            self.branch_files[path.removeprefix(f"{base}/contents/")] = base64.b64decode(
                body["content"]).decode()
            return httpx.Response(201, json={})
        if path == f"{base}/pulls" and m == "GET":
            return httpx.Response(200, json=[{"html_url": u} for u in self.prs])
        if path == f"{base}/pulls" and m == "POST":
            body = json.loads(request.content)
            assert body["head"] == pipeline.BRANCH and body["base"] == "main"
            self.prs.append("https://github.com/ann/shop/pull/7")
            return httpx.Response(201, json={"html_url": self.prs[-1]})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def gh_repo():
    return Repo({**{p: "" for p in PATHS}, **TEXTS, ".github/workflows/ci.yml": SHARED})


@pytest.fixture
def client(tmp_path, prober, gh_repo, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    rt = Runtime(tmp_path / "none.yml", tmp_path / "argus.db", prober=prober)
    transport = httpx.MockTransport(gh_repo)
    poller = Poller(None, transport=transport)
    with TestClient(create_app(rt, start_scheduler=False, poller=poller,
                               github_client=httpx.AsyncClient(transport=transport))) as c:
        csrf = {"X-CSRF-Token": sign_up(c, caplog)["csrf"]}
        assert c.put("/api/github", headers=csrf, json={"token": PAT}).status_code == 200
        yield c, csrf


def test_preview_then_one_click_opens_a_pull_request_argus_never_merges(client, gh_repo):
    c, csrf = client
    v = c.get("/api/github/pipeline/ann/shop").json()
    assert v["kind"] == "shared" and v["repo"] == "ann/shop"
    assert [i["image"] for i in v["images"]] == ["ghcr.io/devinmb/bouy-lab-backend",
                                                 "ghcr.io/devinmb/bouy-lab-frontend"]
    r = c.post("/api/github/pipeline/ann/shop", headers=csrf,
               json={"images": [{"job": "web", "test_command": "npm test"}]})
    assert r.json() == {"url": "https://github.com/ann/shop/pull/7"}
    ci = gh_repo.branch_files[".github/workflows/ci.yml"]
    assert pipeline.kind_of(ci) == "argus" and '"npm test"' in ci
    assert gh_repo.files[".github/workflows/ci.yml"] == SHARED  # main untouched
    # A second click starts the branch over and reuses the open pull request.
    r = c.post("/api/github/pipeline/ann/shop", headers=csrf, json={})
    assert r.json()["url"] == "https://github.com/ann/shop/pull/7" and len(gh_repo.prs) == 1


def test_a_token_that_cannot_write_says_what_it_needs(client, gh_repo):
    c, csrf = client
    gh_repo.write_ok = False
    r = c.post("/api/github/pipeline/ann/shop", headers=csrf, json={})
    assert r.status_code == 502 and "Workflows read and write" in r.json()["detail"]
    assert PAT not in r.text


def test_the_pipeline_needs_the_admin_and_a_repo_argus_can_see(client):
    c, csrf = client
    assert c.post("/api/github/pipeline/ann/shop", json={}).status_code in (401, 403)
    assert c.get("/api/github/pipeline/ann/elsewhere").status_code == 404
    bad = c.post("/api/github/pipeline/ann/shop", headers=csrf,
                 json={"images": [{"job": "web", "image": "docker.io/evil/x"}]})
    assert bad.status_code == 422


async def test_the_app_asks_for_write_only_for_one_repo_and_says_when_it_may_not():
    asked = []

    def gh(request):
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=[{"id": 5, "account": {"login": "ann"},
                                              "html_url": "https://github.com/settings/i/5",
                                              "repository_selection": "selected"}])
        asked.append(json.loads(request.content))
        if len(asked) == 1:
            return httpx.Response(422, json={"message": "permissions not granted"})
        return httpx.Response(201, json={"token": "ghs_one_repo"})

    app = {"id": 77, "slug": "argus-ann", "owner": "ann", "html_url": "https://github.com/apps/a"}
    creds = ghauth.AppCredentials(app, PEM, httpx.AsyncClient(transport=httpx.MockTransport(gh)))
    with pytest.raises(ghauth.NeedsPermission):
        await creds.write_token("ann", "shop")
    assert await creds.write_token("ann", "shop") == "ghs_one_repo"
    assert asked[-1] == {"repositories": ["shop"], "permissions": ghauth.PIPELINE_WRITE}
