"""Updating Argus from its own Settings page (design decision 16).

Argus never touches Docker itself: that would take the Docker socket, root on
its machine. The companion on the machine Argus runs on does it. Argus finds
that host (a container running Argus's image), and when the owner presses
Update Argus, its next signed reply asks for the update. The companion pulls
the image, recreates the stack, waits for Argus's health check, and puts the
old image back if Argus does not come up healthy. Argus restarts in the
middle, so the request is kept in its settings and the new Argus records the
result.

What is newer (decision 21): a released Argus (ARGUS_VERSION 1.2.3) follows the
product repo's GitHub releases of its own major version, which is what its
image tag (:1) pulls; a newer major is only mentioned, since moving to it means
changing the compose file. A development build (ARGUS_VERSION dev) follows the
newest green build on main by commit. Both are public: no GitHub connection
is needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from collections.abc import Callable

import httpx

from argus.settings import Settings

log = logging.getLogger("argus.selfupdate")

REPO = os.environ.get("ARGUS_UPDATE_REPO", "DevinMB/argus")
IMAGE = os.environ.get("ARGUS_IMAGE", "ghcr.io/devinmb/argus")
STATE = "argus.update"  # the request and, later, its result
EVERY = 900  # the Updates page sees a new build within a minute; this should not lag an hour
GIVE_UP = 30 * 60  # a request with no result by then failed


SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def own_commit() -> str:
    return os.environ.get("ARGUS_COMMIT", "unknown")


def own_version() -> str:
    return os.environ.get("ARGUS_VERSION", "dev")


def semver(v: str | None) -> tuple[int, int, int] | None:
    m = SEMVER.match(v or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def text(v: tuple[int, int, int]) -> str:
    return ".".join(map(str, v))


def release_items(body: str) -> list[str]:
    """The pull request titles in GitHub's generated release notes."""
    out = []
    for line in (body or "").splitlines():
        if line.startswith("* "):
            out.append(re.sub(r" by @\S+ in https://\S+$", "", line[2:]).strip())
    return out


class SelfUpdate:
    def __init__(self, settings: Settings, client: httpx.AsyncClient,
                 clock: Callable[[], float]) -> None:
        self.settings, self.client, self.clock = settings, client, clock
        self.latest: str | None = None
        self.changes: list[str] = []
        self.behind = 0
        self.checked_at: float | None = None
        self.error = ""
        self.major: str | None = None  # a newer major release, to move to by hand

    async def check(self) -> None:
        """The newest green build on main and what changed since this one. GitHub's
        API times out now and then: one more try before saying so."""
        for attempt in (1, 2):
            await self._check()
            if not self.error or attempt == 2:
                return
            await asyncio.sleep(3)

    async def _check(self) -> None:
        if (mine := semver(own_version())) is not None:
            return await self._check_releases(mine)
        api = "https://api.github.com/repos/" + REPO
        try:
            r = await self.client.get(f"{api}/actions/runs", params={
                "branch": "main", "status": "success", "event": "push", "per_page": 1},
                timeout=20)
            r.raise_for_status()
            runs = r.json().get("workflow_runs") or []
            self.latest = runs[0]["head_sha"] if runs else None
            self.changes, self.behind = [], 0
            mine = own_commit()
            if self.latest and mine != "unknown" and not self.latest.startswith(mine[:12]):
                c = await self.client.get(f"{api}/compare/{mine}...{self.latest}", timeout=20)
                c.raise_for_status()
                body = c.json()
                self.behind = int(body.get("ahead_by", 0))
                self.changes = [x["commit"]["message"].splitlines()[0]
                                for x in reversed(body.get("commits") or [])][:20]
            self.error = ""
        except (httpx.HTTPError, KeyError, ValueError, IndexError):
            self.error = "GitHub did not answer; try again in a minute"
        self.checked_at = self.clock()

    async def _check_releases(self, mine: tuple[int, int, int]) -> None:
        try:
            r = await self.client.get(f"https://api.github.com/repos/{REPO}/releases",
                                      params={"per_page": 50}, timeout=20)
            r.raise_for_status()
            found = [(v, x) for x in r.json()
                     if not x.get("draft") and not x.get("prerelease")
                     and (v := semver(x.get("tag_name"))) is not None]
            newer = sorted(((v, x) for v, x in found if v[0] == mine[0] and v > mine),
                           key=lambda vx: vx[0], reverse=True)
            self.latest = text(newer[0][0]) if newer else text(mine)
            self.behind = len(newer)
            self.changes = [f"{text(v)}: {item}" for v, x in newer
                            for item in release_items(x.get("body", ""))][:20]
            majors = [v for v, _ in found if v[0] > mine[0]]
            self.major = text(max(majors)) if majors else None
            self.error = ""
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            self.error = "GitHub did not answer; try again in a minute"
        self.checked_at = self.clock()

    async def run_forever(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(EVERY)

    @staticmethod
    def host_of(views: list[dict]) -> tuple[dict, dict] | None:
        """The host whose companion runs Argus, and Argus's container on it."""
        for v in views:
            if v["read_only"] or v["status"] != "ok":
                continue
            for c in (v["report"] or {}).get("containers") or []:
                if c.get("image", "").split(":")[0] == IMAGE and c.get("project"):
                    return v, c
        return None

    def request(self, host: str, container: dict) -> dict:
        req = {"id": secrets.token_hex(8), "host": host, "image": container["image"],
               "project": container["project"], "from": own_commit(), "to": self.latest or "",
               "at": self.clock(), "state": "waiting", "summary": ""}
        self.settings.set(STATE, req)
        return req

    def state(self) -> dict | None:
        s = self.settings.get(STATE)
        if s and s["state"] in ("waiting", "running") and self.clock() - s["at"] > GIVE_UP:
            s = {**s, "state": "failed", "summary": "no word from the companion for 30 minutes"}
            self.settings.set(STATE, s)
        return s

    def to_send(self, host: str) -> dict | None:
        s = self.state()
        if s and s["host"] == host and s["state"] in ("waiting", "running"):
            if s["state"] == "waiting":
                self.settings.set(STATE, {**s, "state": "running"})
            return {k: s[k] for k in ("id", "image", "project")}
        return None

    def result(self, host: str, rep: dict | None) -> None:
        s = self.settings.get(STATE)
        if not rep or not s or s["host"] != host or rep.get("id") != s["id"]:
            return
        if rep.get("state") in ("ok", "failed", "rolled_back") and s["state"] != rep["state"]:
            self.settings.set(STATE, {**s, "state": rep["state"],
                                      "summary": str(rep.get("summary", ""))[:500],
                                      "log": str(rep.get("log", ""))[-8000:],
                                      "finished": self.clock()})
            log.info("Argus update on %s: %s", host, rep["state"])

    def view(self, views: list[dict]) -> dict:
        found = self.host_of(views)
        mine, version = own_commit(), own_version()
        released = semver(version) is not None
        major = version.split(".")[0] if released else "1"
        image = found[1]["image"] if found else f"{IMAGE}:{major}"
        return {"current": mine, "version": version, "released": released, "repo": REPO,
                "latest": self.latest, "behind": self.behind,
                "changes": self.changes, "checked_at": self.checked_at, "error": self.error,
                # A check that failed does not prove anything is up to date.
                "up_to_date": not self.error and bool(self.latest)
                and (self.latest == version if released else self.latest.startswith(mine[:12])),
                "major": self.major,
                # An image pinned to one release (:1.2.3) never moves on a pull.
                "pinned": semver(image.rpartition(":")[2]) is not None,
                "host": found[0]["name"] if found else None,
                "image": image,
                "update": self.state()}
