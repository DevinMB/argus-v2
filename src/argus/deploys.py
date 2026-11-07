"""Deploys: what each host should run, from the deploy repo, sent to its companion.

The deploy repo keeps the layout this homelab's stacks repo already has:

    hosts/<host>            one stack folder per line ("sites/home/shop"); # comments
    sites/home/shop/...     the stack: compose.yml and any files next to it

`<host>` is the companion's name. Argus reads the repo on its default branch
every minute (a folder works too, ARGUS_DEPLOY_DIR, for tests and for people
without GitHub) and puts each host's stacks in the signed reply to its
companion: the files, the stack's secrets as its .env, and a digest. The
companion deploys a stack when its digest changes, after checking it against
the host's own policy, and reports how it went.

Secrets for stacks are kept sealed in Argus, set on the Hosts page, and only
ever sent to the host whose hosts/ file lists the stack.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from argus.ghauth import AppCredentials
from argus.settings import Settings

log = logging.getLogger("argus.deploys")

REPO = "deploy.repo"  # owner/name of the deploy repo
BRANCH = "deploy.branch"  # empty: its default branch
PULL_TOKEN = "deploy.pull_token"  # noqa: S105 (a setting name) secret: registry read token
# secret: a fine-grained token for the deploy repo alone, contents: write. Tag
# bumps use it; the GitHub App stays read-only everywhere, so a compromised
# Argus cannot write to any app's code.
WRITE_TOKEN = "deploy.write_token"  # noqa: S105 (a setting name)
README = """# Stacks deployed by Argus

Each folder under `sites/<host>/` is a Docker Compose stack; `hosts/<host>` lists
the stacks that host runs, one per line. Argus deploys a merged change to the host
that lists it. Secrets are not here: they are kept in Argus and written to each
stack's `.env` on its host.
"""
DEPENDABOT = """# Keeps the images in these stacks up to date: Dependabot opens a pull request
# for each newer version, and Argus deploys it once merged.
version: 2
updates:
  - package-ecosystem: "docker-compose"
    directories: ["/sites/**"]
    schedule:
      interval: "weekly"
"""
BUMP_GRACE = 1800  # seconds after a green build before a missing bump is a failure
SECRETS = "stack.env."  # + stack path: secret JSON {KEY: value}
MAX_FILE = 512 * 1024
MAX_STACK = 4 * 1024 * 1024
STACK = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def env_text(env: dict[str, str]) -> str:
    """A .env compose reads as-is: values single-quoted, so $ and # stay literal."""
    lines = []
    for k in sorted(env):
        v = env[k].replace("\n", "\\n")
        lines.append(f"{k}='{v}'" if "'" not in v else f'{k}="{v}"')
    return "".join(line + "\n" for line in lines)


@dataclass
class Snapshot:
    """The deploy repo at one commit: which host runs what, and the files."""

    commit: str
    hosts: dict[str, list[str]] = field(default_factory=dict)
    # stack path -> {relative file path: bytes}
    stacks: dict[str, dict[str, bytes]] = field(default_factory=dict)
    error: str = ""
    hosts_text: dict[str, str] = field(default_factory=dict)  # hosts/<host> as written
    empty: bool = False  # a new repo with no commit yet: the first write starts it


def image_re(owner: str, repo: str) -> re.Pattern:
    """The repo's images: ghcr.io/<owner>/<repo>[-component]:sha-<commit>. A
    -beta image (shop-beta, or a component's: shop-frontend-beta) follows another
    branch, so it is never main's: see is_beta."""
    return re.compile(rf"(ghcr\.io/{re.escape(owner.lower())}/{re.escape(repo.lower())}"
                      r"(-[a-z0-9-]+)?:sha-)([0-9a-f]{7,40})")


def is_beta(m: re.Match) -> bool:
    return (m.group(2) or "").endswith("-beta")


COMPOSE = ("compose.yml", "compose.yaml", "docker-compose.yml")
OWN = "/var/lib/argus-companion/stacks/"  # where a companion keeps stacks it made itself


def project_name(path: str, files: dict[str, bytes]) -> str:
    """The Compose project name, as Compose and stack-sync work it out: the file's
    top-level `name:`, else the folder's name, lowercased, with only a-z, 0-9, _ and -."""
    for n in COMPOSE:
        if n in files:
            try:
                doc = yaml.safe_load(files[n]) or {}
            except yaml.YAMLError:
                doc = {}
            if isinstance(doc, dict) and isinstance(doc.get("name"), str) and doc["name"]:
                return re.sub(r"[^a-z0-9_-]", "", doc["name"].lower())
            break
    return re.sub(r"[^a-z0-9_-]", "", path.rsplit("/", 1)[-1].lower())


def running_elsewhere(stack: dict, report: dict | None) -> str | None:
    """The folder a stack's project already runs from, when that is not where Argus
    would deploy it. Deploying then would start it again from another folder (and
    lose data kept beside the old one), so such a stack waits to be linked."""
    target = stack.get("dir")
    for c in (report or {}).get("containers") or []:
        wd = (c.get("working_dir") or "").rstrip("/")
        if c.get("project") == stack["name"] and wd and wd != target and not (
                wd + "/").startswith(OWN):
            return wd
    return None


def parse_hosts(name_to_text: dict[str, str]) -> dict[str, list[str]]:
    out = {}
    for name, text in name_to_text.items():
        stacks = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip().strip("/")
            if line and STACK.match(line) and ".." not in line.split("/"):
                stacks.append(line)
        out[name] = stacks
    return out


class DirSource:
    """A deploy repo checked out on disk (ARGUS_DEPLOY_DIR)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def describe(self) -> dict:
        return {"kind": "folder", "where": str(self.root)}

    async def snapshot(self) -> Snapshot:
        hosts_dir = self.root / "hosts"
        texts = {p.name: p.read_text() for p in sorted(hosts_dir.glob("*")) if p.is_file()} \
            if hosts_dir.is_dir() else {}
        hosts = parse_hosts(texts)
        stacks: dict[str, dict[str, bytes]] = {}
        for path in {s for v in hosts.values() for s in v}:
            d = self.root / path
            if d.is_dir():
                stacks[path] = {str(f.relative_to(d)): f.read_bytes()
                                for f in sorted(d.rglob("*"))
                                if f.is_file() and f.stat().st_size <= MAX_FILE}
        digest = hashlib.sha256(json.dumps(
            [hosts, {k: {n: hashlib.sha256(b).hexdigest() for n, b in v.items()}
                     for k, v in stacks.items()}], sort_keys=True).encode()).hexdigest()
        return Snapshot(digest[:12], hosts, stacks, hosts_text=texts)


class GitHubSource:
    """The deploy repo on GitHub, read through the poller's connection. Trees and
    blobs are fetched once per sha; the branch head is an ETag request."""

    def __init__(self, poller, full_name: str, branch: str = "") -> None:
        self.poller, self.full_name, self.branch = poller, full_name, branch
        self._blobs: dict[str, bytes] = {}

    def describe(self) -> dict:
        return {"kind": "github", "repo": self.full_name, "branch": self.branch}

    async def snapshot(self) -> Snapshot:
        base = f"/repos/{self.full_name}"
        branch = self.branch
        if not branch:
            info = await self.poller._get(base)
            if info is None:
                return Snapshot("", error=f"{self.full_name} is not readable with this connection")
            branch = info["default_branch"]
        # GitHub answers 409 for a repo with no commit yet: a new repo, made for Argus.
        head = await self.poller._get(f"{base}/commits/{branch}", missing=(404, 409))
        if head is None and await self.poller._get(f"{base}/commits", missing=(404, 409)) is None:
            return Snapshot("", empty=True)
        if head is None:
            return Snapshot("", error=f"no branch {branch} in {self.full_name}")
        sha = head["sha"]
        tree = await self.poller._get(f"{base}/git/trees/{sha}", recursive="1") or {}
        blobs = {t["path"]: t for t in tree.get("tree", []) if t["type"] == "blob"}

        async def read(entry) -> bytes | None:
            if entry["size"] > MAX_FILE:
                return None
            if entry["sha"] not in self._blobs:
                b = await self.poller._get(f"{base}/git/blobs/{entry['sha']}")
                self._blobs[entry["sha"]] = base64.b64decode(b["content"])
            return self._blobs[entry["sha"]]

        texts = {}
        for path, e in blobs.items():
            if path.startswith("hosts/") and path.count("/") == 1:
                data = await read(e)
                texts[path[6:]] = (data or b"").decode(errors="replace")
        hosts = parse_hosts(texts)
        stacks: dict[str, dict[str, bytes]] = {}
        for stack in {s for v in hosts.values() for s in v}:
            files = {}
            for path, e in blobs.items():
                if path.startswith(stack + "/"):
                    data = await read(e)
                    if data is not None:
                        files[path[len(stack) + 1:]] = data
            stacks[stack] = files
        return Snapshot(sha[:12], hosts, stacks, hosts_text=texts)


class Deploys:
    def __init__(self, settings: Settings, poller, clock: Callable[[], float],
                 folder: Path | None = None) -> None:
        self.settings, self.poller, self.clock, self.folder = settings, poller, clock, folder
        self.snapshot: Snapshot | None = None
        self.read_at: float | None = None
        self.error = ""
        self._source = None
        self._key = None
        self.bump_error = ""

    def source(self):
        if self.folder:
            return DirSource(self.folder)
        repo = self.settings.get(REPO)
        if not repo or not self.poller.connected:
            return None
        key = (repo, self.settings.get(BRANCH, ""), id(self.poller.creds))
        if key != self._key:  # keep the blob cache while nothing changed
            self._source, self._key = GitHubSource(self.poller, repo, key[1]), key
        return self._source

    async def refresh(self) -> None:
        src = self.source()
        if src is None:
            self.snapshot, self.error = None, ""
            return
        try:
            snap = await src.snapshot()
        except (httpx.HTTPError, KeyError, ValueError, OSError) as e:
            self.error = f"{type(e).__name__}: {e}"[:300]
            log.warning("reading the deploy repo failed, keeping the last: %s", self.error)
            return
        if snap.error:
            self.error = snap.error
            return
        self.snapshot, self.read_at, self.error = snap, self.clock(), ""

    async def run_forever(self, every: float = 60) -> None:
        while True:
            try:
                await self.refresh()
                await self.bump_green_builds()
            except Exception:
                log.exception("deploy refresh crashed")
            await asyncio.sleep(every)

    # --- the pipeline: where a repo's images run, and bumping them ---

    def placements(self, owner: str, repo: str) -> list[dict]:
        """Each stack that runs one of the repo's images: its shas and hosts."""
        snap = self.snapshot
        if snap is None:
            return []
        rx, out = image_re(owner, repo), []
        for path, files in snap.stacks.items():
            text = "".join(files.get(n, b"").decode(errors="replace") for n in COMPOSE)
            shas = sorted({m.group(3)[:7] for m in rx.finditer(text) if not is_beta(m)})
            if shas:
                out.append({"path": path, "shas": shas,
                            "hosts": sorted(h for h, st in snap.hosts.items() if path in st)})
        return out

    def bumps(self, builds: list[tuple[str, str, str]]) -> dict[str, str]:
        """New compose texts for green builds: {file path in the repo: text}."""
        snap = self.snapshot
        if snap is None:
            return {}
        changed: dict[str, str] = {}
        for path, files in snap.stacks.items():
            for name in COMPOSE:
                if name not in files:
                    continue
                key = f"{path}/{name}"
                text = changed.get(key, files[name].decode(errors="replace"))
                for owner, repo, sha in builds:
                    text = image_re(owner, repo).sub(
                        lambda m, sha=sha: m.group(0) if is_beta(m)
                        else m.group(1) + sha[:7], text)
                if text != files[name].decode(errors="replace"):
                    changed[key] = text
        return changed

    async def commit(self, files: dict[str, str | None], message: str) -> str:
        """Write files to the deploy repo in one commit, fast-forward only, with
        the deploy repo's own write token. Refuses when the repo moved since
        Argus read it."""
        src, token = self.source(), self.settings.secret(WRITE_TOKEN)
        if not isinstance(src, GitHubSource) or not token:
            raise ValueError("no deploy repo on GitHub with a write token")
        base = f"/repos/{src.full_name}"
        h = {"Authorization": f"Bearer {token}"}
        c = self.poller.client

        async def get(url: str) -> dict:
            return (await c.get(url, headers=h, auth=None)).raise_for_status().json()

        branch = src.branch or (await get(base))["default_branch"]
        ref = await c.get(f"{base}/git/ref/heads/{branch}", headers=h, auth=None)
        if ref.status_code in (404, 409) and self.snapshot is not None and self.snapshot.empty:
            # A new, empty repo: the git API needs a first commit to build on.
            readme = base64.b64encode(README.encode()).decode()
            first = await c.put(f"{base}/contents/README.md", headers=h, auth=None, json={
                "message": "Start the deploy repo", "content": readme, "branch": branch})
            parent = first.raise_for_status().json()["commit"]["sha"]
        else:
            parent = ref.raise_for_status().json()["object"]["sha"]
            if self.snapshot is None or parent[:12] != self.snapshot.commit:
                raise ValueError("the deploy repo changed since Argus read it;"
                                 " try again in a minute")
        commit = await get(f"{base}/git/commits/{parent}")
        tree = (await c.post(f"{base}/git/trees", headers=h, auth=None, json={
            "base_tree": commit["tree"]["sha"],
            # None removes the file (a renamed hosts/ file, say).
            "tree": [{"path": p, "mode": "100644", "type": "blob", "content": t} if t is not None
                     else {"path": p, "mode": "100644", "type": "blob", "sha": None}
                     for p, t in sorted(files.items())]})).raise_for_status().json()
        new = (await c.post(f"{base}/git/commits", headers=h, auth=None, json={
            "message": message, "tree": tree["sha"], "parents": [parent]}))
        new = new.raise_for_status().json()
        # force=false: GitHub refuses unless it is a fast-forward of what we read.
        (await c.patch(f"{base}/git/refs/heads/{branch}", headers=h, auth=None,
                       json={"sha": new["sha"], "force": False})).raise_for_status()
        log.info("committed to %s: %s", src.full_name, message.splitlines()[0])
        return new["sha"]

    def unmatched(self, companions: list[dict]) -> list[dict]:
        """hosts/ files in the repo that name no host Argus knows (a host renamed,
        or one gone), each with the host that runs its stacks, when one does."""
        if self.snapshot is None:
            return []
        names = {c["name"] for c in companions}
        out = []
        for h, stacks in sorted(self.snapshot.hosts.items()):
            if h in names:
                continue
            wanted = {s.rsplit("/", 1)[-1] for s in stacks}
            best, score = None, 0
            for c in companions:
                containers = (c.get("report") or {}).get("containers") or []
                running = {x.get("project") for x in containers}
                n = len(wanted & running)
                if n > score:
                    best, score = c["name"], n
            out.append({"host": h, "stacks": stacks, "suggest": best,
                        "taken": bool(best and best in self.snapshot.hosts)})
        return out

    async def rename_host(self, old: str, new: str | None) -> str:
        """Rename hosts/<old> to hosts/<new> in the repo (appending to new's list
        if it has one), or remove it when new is None."""
        snap = self.snapshot
        if snap is None or old not in snap.hosts_text:
            raise ValueError(f"the deploy repo has no hosts/{old}")
        files: dict[str, str | None] = {f"hosts/{old}": None}
        if new:
            mine = snap.hosts_text[old].rstrip("\n") + "\n"
            theirs = snap.hosts_text.get(new, "")
            files[f"hosts/{new}"] = (theirs.rstrip("\n") + "\n" if theirs.strip() else "") + mine
            msg = f"Rename hosts/{old} to hosts/{new}\n\nArgus knows the host as {new}.\n"
        else:
            msg = f"Remove hosts/{old}\n\nNo host runs it any more.\n"
        sha = await self.commit(files, msg)
        await self.refresh()
        return sha

    async def bump(self, builds: list[tuple[str, str, str]]) -> str | None:
        """Commit the new tags to the deploy repo in one commit. Returns the
        commit, or None when there was nothing to do or the repo moved."""
        changed = self.bumps(builds)
        if not changed or not isinstance(self.source(), GitHubSource) \
                or not self.settings.has_secret(WRITE_TOKEN):
            return None
        what = ", ".join(sorted({f"{repo} sha-{sha[:7]}" for owner, repo, sha in builds
                                 if any(image_re(owner, repo).search(t)
                                        for t in changed.values())}))
        msg = f"Deploy: {what}\n\nTag bump by Argus after a green build on main.\n" + "".join(
            f"- {p}\n" for p in sorted(changed))
        try:
            sha = await self.commit(changed, msg)
        except ValueError:
            return None  # moved meanwhile: the next round reads it again
        await self.refresh()
        return sha

    async def bump_green_builds(self) -> None:
        """Every polled repo whose newest main build is green: its sha goes out."""
        builds = [(t.owner, t.repo.name, s["main"]["sha"])
                  for t in self.poller.targets
                  if (s := self.poller.repos.get(t.name)) and s.get("main")
                  and s["main"]["status"] == "success" and not t.repo.deploy]
        if builds:
            try:
                await self.bump(builds)
                self.bump_error = ""
            except httpx.HTTPError as e:
                self.bump_error = f"tag bump failed: {type(e).__name__}: {e}"[:300]
                log.warning(self.bump_error)

    # --- per host ---

    def env(self, stack: str) -> dict[str, str]:
        raw = self.settings.secret(SECRETS + stack)
        return json.loads(raw) if raw else {}

    def set_env(self, stack: str, changes: dict[str, str | None]) -> None:
        env = self.env(stack)
        for k, v in changes.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        self.settings.set_secret(SECRETS + stack, json.dumps(env) if env else None)

    def stacks_for(self, host: str, report: dict | None = None) -> list[dict]:
        """What a host should run: each stack's files, .env and digest. With the
        host's report, a stack already running from a folder Argus does not manage
        is marked `held` (that folder) and must not be sent until it is linked."""
        snap = self.snapshot
        if snap is None:
            return []
        out = []
        for path in snap.hosts.get(host, []):
            files = snap.stacks.get(path)
            if not files or "compose.yml" not in files and "compose.yaml" not in files \
                    and "docker-compose.yml" not in files:
                continue
            if sum(len(b) for b in files.values()) > MAX_STACK:
                continue
            env = self.env(path)
            digest = hashlib.sha256(json.dumps(
                [{n: hashlib.sha256(b).hexdigest() for n, b in files.items()}, env],
                sort_keys=True).encode()).hexdigest()[:16]
            stack = {"path": path, "name": project_name(path, files), "digest": digest,
                     "files": {n: base64.b64encode(b).decode() for n, b in files.items()},
                     "env": env_text(env)}
            # Adopted: it lives in its own folder on the host (see adopt.py).
            where = self.settings.get("stack.dir." + path)
            if where:
                stack["dir"] = where
            held = running_elsewhere(stack, report)
            if held:
                stack["held"] = held
            out.append(stack)
        return out

    async def pull_credential(self) -> dict | None:
        """Read access to the registry, for this deploy only: the read token the owner
        saved, when there is one (it is there because the app's token was refused:
        a GitHub App cannot read a personal account's packages), else the app's
        short-lived installation token."""
        creds = self.poller.creds
        repo = self.settings.get(REPO) or ""
        saved = self.settings.secret(PULL_TOKEN)
        if saved:
            return {"registry": "ghcr.io", "username": "argus", "password": saved}
        if isinstance(creds, AppCredentials) and repo:
            try:
                token = await creds.pull_token(repo.split("/")[0])
            except httpx.HTTPError as e:
                log.warning("no pull token from the app: %s", e)
                token = None
            if token:
                return {"registry": "ghcr.io", "username": "x-access-token", "password": token}
        return None

    def views(self, companions: list[dict]) -> list[dict]:
        """For the Hosts page: each host's stacks and how their deploys went."""
        out = []
        for c in companions:
            reported = {d["path"]: d for d in ((c.get("report") or {}).get("deploys") or [])}
            stacks = []
            for s in self.stacks_for(c["name"], c.get("report")):
                r = reported.get(s["path"])
                if c["read_only"]:
                    status = "read_only"
                elif s.get("held"):
                    status = "link"
                elif r is None or r.get("digest") != s["digest"]:
                    status = "pending"
                else:
                    status = {"ok": "deployed"}.get(r.get("result"), r.get("result", "pending"))
                stacks.append({"path": s["path"], "name": s["name"], "digest": s["digest"],
                               "status": status, "held": s.get("held"),
                               "detail": (r or {}).get("detail", ""),
                               "at": (r or {}).get("at"),
                               "secrets": sorted(self.env(s["path"]))})
            out.append({"companion": c["name"], "stacks": stacks})
        return out
