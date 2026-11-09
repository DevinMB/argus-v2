"""The app pipeline as GitHub sees it, for the Updates view and each tile's
track (decision 0010). Polled every `github.poll` seconds (a minute when the
repos come from "Connect GitHub") and held in memory: a restart only waits for
the next poll. Responses are cached by ETag; GitHub does not count a 304
against the rate limit, so a quiet minute costs almost nothing.

Which repos: the `repos` in argus.yml when there are any (this homelab), else
every repo the connection can see, less the archived ones and those hidden on
the GitHub page. argus.yml can still annotate a discovered repo (its check).

Every step of a track comes from a real event: a PR, a workflow run, a commit
in the stacks repo, a stack-sync heartbeat, the check itself. A step whose
pipeline does not exist yet says "not wired", never green.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from argus import pipeline
from argus.config import GitHub, Repo
from argus.ghauth import REDISCOVER, Credentials, StaticToken

log = logging.getLogger("argus.github")

API = "https://api.github.com"
GATE = "github-actions[bot]"  # the Dependabot merge gate merges as this
DEPENDABOT = "dependabot[bot]"
WAITS = {"waits-for-devin", "waits-for-you"}  # the gate labels majors (old name, Argus's)
AUTO = {GATE, DEPENDABOT}  # merged by a bot: auto-merge
DEFAULT_POLL = 60
VERSIONS = re.compile(r"\bfrom v?(\d+)(?:\.(\d+))?\S* to v?(\d+)(?:\.(\d+))?")
MERGED_WINDOW = 14 * 86400
BUMP_GRACE = 1800  # seconds after a green build before a missing bump is a failure
SLOW = 900  # seconds a step may wait before the track says it is taking long
STUCK = 3600  # seconds a green patch or minor PR may stay open before it needs a look
FAILS_SHOWN = 3  # failed polls in a row before the UI says so (one is a GitHub blip)
BUILD_EVENTS = ("push", "workflow_dispatch")
PR_REF = re.compile(r"\(#(\d+)\)|Merge pull request #(\d+)")
BAD = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}


def ts(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:300]


def is_major(title: str) -> bool:
    """A Dependabot title that crosses a major version ("from 19.2.0 to 20.0.0").
    Below 1.0, a minor bump counts as major, as semver says."""
    m = VERSIONS.search(title)
    if not m:
        return False
    a, a2, b, b2 = m.groups()
    return a != b or (a == "0" and (a2 or "0") != (b2 or "0"))


@dataclass(frozen=True)
class Target:
    """One repo to poll: where it lives and what argus.yml says about it."""

    owner: str
    repo: Repo
    branch: str = "main"
    key: str = ""  # the name the UI shows: the repo's, or owner/repo when owners mix
    # Listed in argus.yml, which says how it deploys; a discovered repo's deploy
    # is unknown until companions deploy it (M4).
    configured: bool = True

    @property
    def name(self) -> str:
        return self.key or self.repo.name


class _Auth(httpx.Auth):
    """Each request carries the token for the repo's owner (one per app installation)."""

    def __init__(self, poller: Poller) -> None:
        self.poller = poller

    async def async_auth_flow(self, request):
        parts = request.url.path.split("/")
        owner = parts[2] if len(parts) > 2 and parts[1] == "repos" else None
        creds = self.poller.creds
        token = await creds.token_for(owner) if creds else None
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request


class Poller:
    def __init__(self, creds: Credentials | str | None, clock: Callable[[], float] = time.time,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.clock = clock
        self.client = httpx.AsyncClient(
            base_url=API, timeout=20, transport=transport, auth=_Auth(self),
            headers={"Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "argus"},
        )
        self.creds: Credentials | None = (StaticToken(creds, self.client, "file")
                                          if isinstance(creds, str) else creds)
        self.repos: dict[str, dict] = {}
        self.targets: list[Target] = []
        self.found: list[dict] | None = None  # every repo the connection sees
        self.found_at = 0.0
        self.found_error = ""
        self.etags: dict[str, tuple[str, Any]] = {}
        self.rate: tuple[int, float] | None = None  # (remaining, reset time)
        # (repo, PR) -> who merged it. The PR list leaves merged_by out; a merged
        # PR never changes, so each is fetched once.
        self.merged_by: dict[tuple[str, int], str | None] = {}
        self.last_success: float | None = None
        self.last_error = ""
        self.failures = 0  # failed polls in a row

    async def aclose(self) -> None:
        await self.client.aclose()

    @property
    def connected(self) -> bool:
        return self.creds is not None

    def connect(self, creds: Credentials | None) -> None:
        """A new connection from the GitHub page: forget what the old one saw."""
        self.creds, self.found, self.found_at, self.etags = creds, None, 0.0, {}
        self.repos, self.targets, self.last_success, self.last_error = {}, [], None, ""
        self.failures = 0

    async def _get(self, url: str, missing: tuple[int, ...] = (404,), **params) -> Any:
        """JSON for a GET; None when the status is in `missing`. Unchanged answers
        come from the ETag cache."""
        key = str(httpx.URL(url, params=params))
        cached = self.etags.get(key)
        headers = {"If-None-Match": cached[0]} if cached else {}
        r = await self.client.get(url, params=params, headers=headers)
        if "x-ratelimit-remaining" in r.headers:
            self.rate = (int(r.headers["x-ratelimit-remaining"]),
                         float(r.headers.get("x-ratelimit-reset", 0)))
        if r.status_code == 304 and cached:
            return cached[1]
        if r.status_code in missing:
            return None
        r.raise_for_status()
        data = r.json()
        if r.headers.get("etag"):
            self.etags[key] = (r.headers["etag"], data)
        return data

    async def discover(self, force: bool = False) -> list[dict]:
        """Every repo the connection sees, refreshed every ten minutes."""
        if self.creds and (force or self.found is None
                           or self.clock() - self.found_at > REDISCOVER):
            try:
                self.found = sorted(await self.creds.repos(), key=lambda r: r["full_name"].lower())
                self.found_at, self.found_error = self.clock(), ""
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
                self.found_error = _err(e)
                log.warning("listing GitHub repos failed: %s", self.found_error)
        return self.found or []

    async def targets_for(self, config, hidden: set[str]) -> list[Target]:
        """argus.yml's repos when it lists any, else every repo the connection
        sees that is not archived or hidden."""
        if config.github and config.repos:
            return [Target(config.github.owner, r) for r in config.repos]
        notes = {r.name.lower(): r for r in config.repos}
        shown = [r for r in await self.discover()
                 if not r["archived"] and r["full_name"] not in hidden]
        mixed = len({r["owner"].lower() for r in shown}) > 1
        out = []
        for r in shown:
            note = notes.get(r["full_name"].lower()) or notes.get(r["name"].lower())
            repo = Repo(name=r["name"], check=note.check if note else "",
                        deploy=note.deploy if note else "")
            out.append(Target(r["owner"], repo, r["default_branch"],
                              r["full_name"] if mixed else r["name"], configured=False))
        return out

    async def poll(self, gh: GitHub | None, repos: Sequence[Repo | Target]) -> None:
        """Refresh every repo. On any error the previous results stay."""
        targets = [t if isinstance(t, Target) else Target(gh.owner, t) for t in repos]
        try:
            hosts = await self._hosts(gh) if gh and any(t.repo.stack for t in targets) else {}
            states = await asyncio.gather(*(self._repo(gh, t, hosts) for t in targets))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            self.last_error, self.failures = _err(e), self.failures + 1
            log.warning("github poll failed, keeping the last results: %s", self.last_error)
            return
        self.repos = {s["name"]: s for s in states}
        self.targets = targets
        self.last_success, self.last_error, self.failures = self.clock(), "", 0

    @property
    def shown_error(self) -> str:
        """The poll error worth showing: GitHub has a bad minute now and then, so
        only after FAILS_SHOWN failures in a row, or before any poll worked."""
        return self.last_error if self.failures >= FAILS_SHOWN or not self.last_success else ""

    async def _file(self, gh: GitHub, path: str) -> str | None:
        f = await self._get(f"/repos/{gh.owner}/{gh.stacks_repo}/contents/{path}")
        return base64.b64decode(f["content"]).decode() if f else None

    async def _hosts(self, gh: GitHub) -> dict[str, str]:
        """Stack path -> the host whose hosts/<hostname> lists it."""
        listing = await self._get(f"/repos/{gh.owner}/{gh.stacks_repo}/contents/hosts") or []
        out = {}
        for f in listing:
            if f["type"] != "file":
                continue
            for line in (await self._file(gh, f["path"]) or "").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    out[line] = f["name"]
        return out

    async def _repo(self, gh: GitHub | None, t: Target, hosts: dict[str, str]) -> dict:
        repo = t.repo
        base = f"/repos/{t.owner}/{repo.name}"
        open_prs = await self._get(f"{base}/pulls", state="open", per_page=50) or []
        closed = await self._get(f"{base}/pulls", state="closed", sort="updated",
                                 direction="desc", per_page=30) or []
        # ci.yml when the repo has one (the shared workflows' name), else the
        # newest push build on the default branch, whatever its workflow.
        runs = await self._get(f"{base}/actions/workflows/ci.yml/runs", branch=t.branch,
                               per_page=10)
        has_ci = runs is not None
        if not has_ci:
            runs = await self._get(f"{base}/actions/runs", branch=t.branch, per_page=20)
        run = next((x for x in (runs or {}).get("workflow_runs", [])
                    if x["event"] in BUILD_EVENTS), None)
        if has_ci:
            # A newer commit built only by another workflow (a merge gate that
            # dispatches a release rather than CI) is still main's newest build.
            every = await self._get(f"{base}/actions/runs", branch=t.branch, per_page=20)
            newest = next((x for x in (every or {}).get("workflow_runs", [])
                           if x.get("event") in BUILD_EVENTS), None)
            if newest and (run is None or (newest["head_sha"] != run["head_sha"]
                                           and ts(newest["created_at"]) > ts(run["created_at"]))):
                run = newest
        # Dependabot's version updates run as "dynamic" workflow runs.
        dyn = await self._get(f"{base}/actions/runs", event="dynamic", per_page=30)
        # 403 too: Dependabot alerts off, or not readable with this token.
        alerts = await self._get(f"{base}/dependabot/alerts", missing=(403, 404),
                                 state="open", per_page=100)
        # Which pipeline builds it (decision 20); the ETag makes a re-read free.
        pipe = {"kind": "none", "version": None}
        if has_ci:
            f = await self._get(f"{base}/contents/.github/workflows/ci.yml", ref=t.branch)
            text = base64.b64decode((f or {}).get("content", "")).decode(errors="replace")
            pipe = {"kind": pipeline.kind_of(text), "version": pipeline.version_of(text)}
        now = self.clock()
        merged = [p for p in closed if p["merged_at"] and now - ts(p["merged_at"]) < MERGED_WINDOW]
        for p in merged:
            await self._merger(base, repo.name, p["number"])
        main = await self._main(base, repo.name, run) if run else None
        return {
            "name": t.name,
            "branch": t.branch,
            "configured": t.configured,
            "url": f"https://github.com/{t.owner}/{repo.name}",
            "check": repo.check or None,
            "main": main,
            "updates": [await self._update(base, p) for p in open_prs
                        if p["user"]["login"] == DEPENDABOT],
            "merged": [
                {"number": p["number"], "title": p["title"], "url": p["html_url"],
                 "merged_at": ts(p["merged_at"]),
                 "by": self.merged_by.get((repo.name, p["number"])),
                 "dependabot": p["user"]["login"] == DEPENDABOT}
                for p in merged
            ],
            "alerts": None if alerts is None else [
                {"severity": a["security_vulnerability"]["severity"],
                 "package": a["security_vulnerability"]["package"]["name"],
                 "summary": a["security_advisory"]["summary"],
                 "created_at": ts(a["created_at"]), "url": a["html_url"]}
                for a in alerts
            ],
            "stack": await self._stack(gh, repo, hosts) if gh and repo.stack else None,
            "dependabot": dependabot_runs((dyn or {}).get("workflow_runs", [])),
            "pipeline": pipe,
            "owner": t.owner, "repo": repo.name,
        }

    async def _merger(self, base: str, repo: str, number: int) -> str | None:
        key = (repo, number)
        if key not in self.merged_by:
            pr = await self._get(f"{base}/pulls/{number}")
            self.merged_by[key] = ((pr or {}).get("merged_by") or {}).get("login")
        return self.merged_by[key]

    async def _main(self, base: str, repo: str, run: dict) -> dict:
        jobs = (await self._get(f"{base}/actions/runs/{run['id']}/jobs") or {}).get("jobs", [])
        steps = [(j["name"], s) for j in jobs for s in j.get("steps", [])]
        trivy = next((s["conclusion"] for _, s in steps if s["name"].startswith("Trivy")), None)
        failed = next((f"{j}: {s['name']}" for j, s in steps if s["conclusion"] in BAD), None)
        message = (run.get("head_commit") or {}).get("message", "")
        m = PR_REF.search(message)
        pr = int(m.group(1) or m.group(2)) if m else None
        if run["status"] != "completed":
            status = "running"
        else:
            status = "success" if run["conclusion"] == "success" else "failure"
        return {
            "sha": run["head_sha"][:7], "status": status, "url": run["html_url"],
            # Without the "(#12)" a squash merge appends; the track shows the PR itself.
            "title": re.sub(r"\s*\(#\d+\)$", "", message.splitlines()[0]) if message else "",
            "pr": pr, "merged_by": await self._merger(base, repo, pr) if pr else None,
            "started_at": ts(run.get("run_started_at") or run["created_at"]),
            "updated_at": ts(run["updated_at"]),
            "trivy": trivy, "failed_step": failed,
        }

    async def _update(self, base: str, pr: dict) -> dict:
        runs = (await self._get(f"{base}/actions/runs", head_sha=pr["head"]["sha"], per_page=20)
                or {}).get("workflow_runs", [])
        if not runs:
            ci = "none"
        elif any(r["status"] != "completed" for r in runs):
            ci = "running"
        elif any(r["conclusion"] in BAD for r in runs):
            ci = "failure"
        else:
            ci = "success"
        return {"number": pr["number"], "title": pr["title"], "url": pr["html_url"],
                "created_at": ts(pr["created_at"]), "ci": ci,
                "waits_for_you": is_major(pr["title"])
                or any(label["name"] in WAITS for label in pr["labels"])}

    async def _stack(self, gh: GitHub, repo: Repo, hosts: dict[str, str]) -> dict:
        path = f"{repo.stack}/compose.yml"
        text = await self._file(gh, path) or ""
        # <repo> or <repo>-<component>; -beta follows another branch, so it is not main's.
        tag = re.compile(rf"ghcr\.io/{re.escape(gh.owner.lower())}/{re.escape(repo.name.lower())}"
                         r"(-[a-z0-9-]+)?:sha-([0-9a-f]{7,40})")
        shas = sorted({m.group(2)[:7] for m in tag.finditer(text) if m.group(1) != "-beta"})
        commits = await self._get(f"/repos/{gh.owner}/{gh.stacks_repo}/commits",
                                  path=path, per_page=1) or []
        last = commits[0] if commits else None
        return {
            "path": repo.stack, "host": hosts.get(repo.stack), "shas": shas,
            "changed_at": last and ts(last["commit"]["committer"]["date"]),
            # Any change to the stack: this app's tag, or another image in it (cloudflared).
            "change": last and (last["commit"].get("message") or "").partition("\n")[0],
            "url": f"https://github.com/{gh.owner}/{gh.stacks_repo}/blob/main/{path}",
        }


def dependabot_runs(runs: list[dict]) -> dict | None:
    """When Dependabot last looked for new versions, and whether that look failed.
    One look is a batch of runs, one per ecosystem, started within the hour."""
    ups = [r for r in runs if r.get("path", "").startswith("dynamic/dependabot/dependabot-updates")]
    if not ups:
        return None
    newest = max(ts(r["created_at"]) for r in ups)
    batch = [r for r in ups if newest - ts(r["created_at"]) < 3600]
    bad = next((r for r in batch if r["conclusion"] in BAD), None)
    return {"last_run": newest, "failed": bad and bad["name"], "url": bad and bad["html_url"]}


async def run_forever(poller: Poller, config: Callable[[], Any],
                      hidden: Callable[[], set[str]] = set) -> None:
    """Poll on the configured interval; the config is re-read each round. Low on
    rate limit, it waits for the reset rather than fail half a poll."""
    while True:
        cfg = config()
        if poller.connected:
            try:
                targets = await poller.targets_for(cfg, hidden())
                await poller.poll(cfg.github, targets)
            except Exception:
                log.exception("github poll crashed")
        wait = cfg.github.poll if cfg.github else DEFAULT_POLL
        if poller.rate and poller.rate[0] < 100:
            wait = max(wait, poller.rate[1] - time.time() + 5)
            log.warning("GitHub rate limit nearly used up; next poll in %ds", wait)
        await asyncio.sleep(wait)


def _for(seconds: float) -> str:
    """How long ago, in the words a track uses: "under a minute", "6m", "2h 5m"."""
    m = int(seconds // 60)
    if m < 1:
        return "under a minute"
    return f"{m}m" if m < 60 else f"{m // 60}h {m % 60}m" if m % 60 else f"{m // 60}h"


def _waiting(seconds: float, what: str) -> str:
    """A waiting step's detail, flagged once it has waited longer than usual."""
    return f"Taking longer than usual: {what}" if seconds > SLOW else what


def _placed(add, placed: list[dict], main: dict | None, built: str, now: float,
            repo_name: str, can_bump: bool) -> bool:
    """Bumped and deployed steps from the deploy repo and companions' reports."""
    if built != "success":
        add("bumped", "pending", "Waits for a green build on main")
        add("deployed", "pending", "Waits for the tag bump")
        return False
    sha = main["sha"]
    behind = [p["path"] for p in placed if sha not in p["shas"]]
    if not behind:
        add("bumped", "done", f"sha-{sha} in {repo_name}")
    elif not can_bump:
        add("bumped", "not_wired", "No write token for the deploy repo, so Argus does not bump "
            "tags; add one on the GitHub page")
    elif now - main["updated_at"] < BUMP_GRACE:
        age = now - main["updated_at"]
        add("bumped", "running", _waiting(age, f"Built {_for(age)} ago; Argus puts sha-{sha} "
                                          f"in {repo_name} within a minute"))
    else:
        add("bumped", "failed", f"{', '.join(behind)} still on an older sha 30 minutes after "
            "the build; the GitHub page says why")
    if behind:
        add("deployed", "pending", "Waits for the tag bump")
        return False
    hosts = [(h, st) for p in placed for h, st in p["status"].items()]
    if not hosts:
        add("deployed", "not_wired", "No hosts/ file in the deploy repo lists its stack")
        return False
    bad = [h for h, st in hosts if st in ("failed", "refused", "rolled_back")]
    waiting = [h for h, st in hosts if st not in ("deployed", "read_only")]
    if bad:
        add("deployed", "failed", f"Deploy failed on {', '.join(bad)}; see Hosts")
        return False
    if waiting:
        add("deployed", "running", f"Deploying sha-{sha} on {', '.join(waiting)}")
        return False
    add("deployed", "done", f"On {', '.join(h for h, _ in hosts)} by its companion")
    return True


TITLES = {"updates": "Update PRs", "merged": "Merged to main", "built": "Built and scanned",
          "bumped": "Tag bumped", "deployed": "Deployed", "healthy": "Healthy"}


def _who(main: dict) -> str:
    if main["pr"] is None:
        return "pushed directly"
    by = "the gate" if main["merged_by"] in AUTO else main["merged_by"]
    return f"#{main['pr']}, merged by {by}" if by else f"#{main['pr']}"


def track(state: dict, check: dict | None, beats: dict[str, dict], now: float,
          stacks_repo: str = "homelab-stacks", deploy: str = "",
          placed: list[dict] | None = None, can_bump: bool = True) -> list[dict]:
    """The steps a change takes from update PR to healthy, for the newest change on main.

    Statuses: done, running, waiting (for the owner), failed, pending (an earlier
    step has not finished), not_wired (that part of the pipeline does not exist yet),
    paused (its check is paused by the owner).
    A repo deployed by its own timer (`deploy`, a heartbeat) has no tag bump; one
    deployed by nothing (a library shipped inside another app) ends at the build.
    `placed` is v2's way: the deploy repo's stacks that run the repo's images,
    each with its hosts and how each host's companion deployed it.
    """
    steps = []

    def add(key, status, detail, url=None, at=None):
        steps.append({"key": key, "title": TITLES[key], "status": status, "detail": detail,
                      "url": url, "at": at})

    ups = state["updates"]
    dep = state.get("dependabot")
    n = len(state.get("alerts") or [])
    alerts = f". {n} open security alert{'s' if n > 1 else ''}" if n else ""
    failing = [p for p in ups if p["ci"] == "failure" and not p["waits_for_you"]]
    waiting = [p for p in ups if p["waits_for_you"]]
    # Green for an hour and not merged: the gate is missing or broken in this
    # repo (mars and mars-connectors had none, 2026-11-02).
    stuck = [p for p in ups if p["ci"] == "success" and not p["waits_for_you"]
             and now - p.get("created_at", now) > STUCK]
    more = lambda n: f" (+{n} more)" if n > 1 else ""  # noqa: E731
    if failing:
        add("updates", "failed", f"{failing[0]['title']}: CI failed{more(len(failing))}",
            failing[0]["url"])
    elif stuck:
        add("updates", "waiting",
            f"{stuck[0]['title']} passed CI {_for(now - stuck[0]['created_at'])} ago and did not "
            f"merge{more(len(stuck))}. Is the Dependabot merge gate in this repo's CI?",
            stuck[0]["url"])
    elif waiting:
        add("updates", "waiting", f"{waiting[0]['title']} waits for you{more(len(waiting))}",
            waiting[0]["url"])
    elif ups:
        add("updates", "running", f"{len(ups)} in CI; the gate merges patch and minor on green",
            ups[0]["url"])
    elif dep and dep["failed"]:
        add("updates", "failed", f"Dependabot's last look failed: {dep['failed']}", dep["url"],
            dep["last_run"])
    else:
        looked = "Dependabot last looked for new versions" if dep else "No Dependabot runs seen"
        add("updates", "done", f"No open update PRs. {looked}{alerts}", at=dep and dep["last_run"])

    main = state["main"]
    built = main["status"] if main else "none"
    if main is None:
        add("merged", "not_wired", f"No CI runs on {state.get('branch', 'main')}")
    else:
        pr_url = state["url"] + (f"/pull/{main['pr']}" if main["pr"] else "")
        add("merged", "done", f"{main['title']} ({_who(main)})", pr_url, main["started_at"])
        if built == "running":
            add("built", "running", f"Building {main['sha']}", main["url"], main["started_at"])
        elif built == "success":
            scan = {"success": "Trivy clean", None: "no Trivy step"}.get(
                main["trivy"], f"Trivy {main['trivy']}")
            add("built", "done", f"sha-{main['sha']}, {scan}", main["url"], main["updated_at"])
        else:
            add("built", "failed", f"Failed at {main['failed_step'] or 'an unknown step'}",
                main["url"], main["updated_at"])

    stack = state["stack"]
    deployed = False
    if stack is None and deploy:
        hb = beats.get(deploy)
        running = (hb or {}).get("version") or ""
        if built != "success":
            add("deployed", "pending", "Waits for a green build on main")
        elif hb is None:
            add("deployed", "not_wired", f"No {deploy} heartbeat")
        elif running[:7] == main["sha"]:
            deployed = True
            add("deployed", "done", f"sha-{main['sha']} is running, deployed by {deploy}",
                at=hb["version_at"])
        elif hb["status"] == "late":
            add("deployed", "failed", f"{deploy} is late; see journalctl -u {deploy}")
        elif running:
            age = now - main["updated_at"]
            # Not flagged as slow: a deploy timer keeps its own schedule.
            add("deployed", "running", f"Built {_for(age)} ago; waiting for {deploy} to deploy "
                f"sha-{main['sha']} (it still runs sha-{running[:7]}; journalctl -u {deploy} "
                "says when)")
        else:
            add("deployed", "not_wired", f"{deploy} does not report which version it runs")
    elif stack is None and placed:
        deployed = _placed(add, placed, main, built, now, stacks_repo, can_bump)
    elif stack is None:
        pass
    else:
        bumped = built == "success" and main["sha"] in stack["shas"]
        if built != "success":
            add("bumped", "pending", "Waits for a green build on main")
        elif bumped:
            # No time: the stack's last commit may be another image's bump.
            add("bumped", "done", f"sha-{main['sha']} in {stacks_repo}", stack["url"])
        elif now - main["updated_at"] < BUMP_GRACE:
            age = now - main["updated_at"]
            add("bumped", "running", _waiting(age, (
                f"Built {_for(age)} ago; waiting for stack-bump to put sha-{main['sha']} "
                f"in {stacks_repo} (it runs every 5 minutes)")), stack["url"])
        else:
            tags = ", ".join("sha-" + s for s in stack["shas"]) or "no tag"
            add("bumped", "failed", f"Still {tags} 30 minutes after the build; "
                "see journalctl -u stack-bump", stack["url"])

        host = stack["host"]
        hb = beats.get(f"stack-sync-{host}") if host else None
        if not bumped:
            add("deployed", "pending", "Waits for the tag bump")
        elif host is None:
            add("deployed", "failed", f"No hosts/ file in {stacks_repo} lists it")
        elif hb is None:
            add("deployed", "not_wired", f"No stack-sync-{host} heartbeat")
        elif hb["last_seen"] and hb["last_seen"] >= (stack["changed_at"] or 0):
            deployed = True
            last = f". Last stack change: {stack['change']}" if stack.get("change") else ""
            add("deployed", "done", f"On {host} by stack-sync{last}", stack["url"],
                stack["changed_at"])
        elif hb["status"] == "late":
            add("deployed", "failed", f"stack-sync-{host} is late; see journalctl -u stack-sync")
        else:
            age = now - (stack["changed_at"] or now)
            add("deployed", "running", _waiting(age, (
                f"sha-{main['sha']} went into {stacks_repo} {_for(age)} ago; waiting for "
                f"stack-sync on {host} to deploy it (it runs every 5 minutes)")))

    if check is None and stack is None and not deploy and not placed:
        pass
    elif check is None:
        add("healthy", "not_wired",
            "No check found: name one after the repo, or link one in argus.yml")
    elif (stack is not None or deploy or placed) and not deployed:
        add("healthy", "pending", f"{check['name']} is {check['status']} on the previous deploy")
    elif check["status"] == "paused":
        why = (check.get("paused") or {}).get("reason")
        add("healthy", "paused", f"{check['name']} is paused{': ' + why if why else ''}",
            at=(check.get("paused") or {}).get("since"))
    elif check["status"] == "up":
        add("healthy", "done", f"{check['name']} is up, with no failure since",
            at=check.get("last_change"))
    elif check["status"] == "unknown":
        add("healthy", "running", f"{check['name']} has no result yet")
    else:
        why = f": {check['last_error']}" if check.get("last_error") else ""
        add("healthy", "failed", f"{check['name']} is {check['status']}{why}")
    return steps


def check_for(repo: str, checks: Sequence) -> str:
    """The check that says whether a repo's app is healthy, when argus.yml does
    not name one: the check named after the repo, else the only one named
    "<repo>-..." (the one that alerts, when several do not). "" if unsure."""
    name = repo.lower().rsplit("/", 1)[-1]
    by = {c.name: c for c in checks}
    if name in by:
        return name
    near = [c for c in checks if c.name.startswith(name + "-")]
    alerting = [c for c in near if c.severity == "critical"]
    pick = near if len(near) == 1 else alerting if len(alerting) == 1 else []
    return pick[0].name if pick else ""
