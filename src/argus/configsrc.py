"""Where argus.yml lives, and editing it from the UI.

Three sources:

- `argus` (a fresh install's default): the text is kept in Argus's database.
- `github`: a file in a repo (this homelab's homelab-ops/argus/argus.yml).
  Argus reads it every minute, so a change made in git shows within a
  minute. A UI edit becomes a commit, or a PR when the owner prefers reviews,
  written with the write token (the one for the repos Argus writes to).
- `file`: the mounted ARGUS_CONFIG, as v1. Read-only in the UI.

Edits are made on the YAML text with a round-trip parser, so comments, order
and layout survive and an edit changes only the lines it touches. Every
result is validated before it is saved. An edit names the version it started
from; if the config changed meanwhile, it is refused and the UI shows the
newer one.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import time
from typing import Any

import httpx
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from argus import config as cfg
from argus.deploys import WRITE_TOKEN
from argus.settings import Settings

log = logging.getLogger("argus.config")

SOURCE = "config.source"
TEXT = "config.text"
REPO, PATH, BRANCH, MODE = "config.repo", "config.path", "config.branch", "config.mode"
KINDS = {"checks": cfg.Check, "heartbeats": cfg.Heartbeat, "jobs": cfg.Job}

STARTER = """\
# Argus's config. Edit it here or on the Checks page; Argus checks it before saving.
# Docs: the README's "Config" section.
checks: []
"""


class EditError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def validate(text: str) -> cfg.Config:
    try:
        return cfg.parse(text)
    except Exception as e:  # yaml and pydantic errors, shown to the editor
        raise EditError(f"not saved: {e}") from None


def _compact(kind: str, entry: dict, sites: dict, like: list) -> CommentedMap:
    """The entry as the file should hold it: only what differs from the defaults,
    with its keys in the order the file's other entries use."""
    model = KINDS[kind].model_validate(entry)
    data = model.model_dump(mode="json", exclude_defaults=True)
    if len(sites) <= 1:
        data.pop("site", None)
    order: list[str] = ["name", "kind", "target"]
    for e in like:
        order += [k for k in e if k not in order]
    ordered = {k: data[k] for k in order if k in data} | {
        k: v for k, v in data.items() if k not in order}
    # Quote a value where the file quotes that key (target: "https://..."), and
    # write whole numbers as such (timeout: 15, not 15.0).
    quoted = {k for e in like for k, v in e.items() if isinstance(v, DoubleQuotedScalarString)}
    for k, v in ordered.items():
        if isinstance(v, float) and v.is_integer():
            ordered[k] = int(v)
        elif isinstance(v, str) and k in quoted:
            ordered[k] = DoubleQuotedScalarString(v)
    return CommentedMap(ordered)


def upsert(text: str, kind: str, entry: dict, replace: str | None = None) -> str:
    """Add an entry (after the last of its group) or replace the one named
    `replace`. Returns the new text, validated."""
    y = _yaml()
    doc = y.load(text) or CommentedMap()
    current = validate(text) if text.strip() else cfg.Config()
    seq = doc.get(kind)
    if seq is None:
        seq = doc[kind] = CommentedSeq()
    new = _compact(kind, entry, current.sites, list(seq))
    flow = len(seq) == 0 or getattr(seq[0], "fa", None) is not None and seq[0].fa.flow_style()
    if flow:
        new.fa.set_flow_style()
    names = [e.get("name") for e in seq]
    if replace is not None:
        if replace not in names:
            raise EditError(f"no {kind[:-1]} named {replace}; someone may have removed it", 409)
        seq[names.index(replace)] = new
    else:
        if new["name"] in names:
            raise EditError(f"there is already a {kind[:-1]} named {new['name']}")
        group = entry.get("group", "default")
        same = [i for i, e in enumerate(seq) if e.get("group", "default") == group]
        seq.insert(same[-1] + 1 if same else len(seq), new)
    out = io.StringIO()
    y.dump(doc, out)
    validate(out.getvalue())
    return out.getvalue()


def remove(text: str, kind: str, name: str) -> str:
    y = _yaml()
    doc = y.load(text) or CommentedMap()
    seq = doc.get(kind) or []
    names = [e.get("name") for e in seq]
    if name not in names:
        raise EditError(f"no {kind[:-1]} named {name}", 404)
    del seq[names.index(name)]
    out = io.StringIO()
    y.dump(doc, out)
    validate(out.getvalue())
    return out.getvalue()


class ConfigSource:
    def __init__(self, rt, settings: Settings, poller, clock=time.time) -> None:
        self.rt, self.settings, self.poller, self.clock = rt, settings, poller, clock
        self.sha: str | None = None  # the git blob the running config came from
        self.error = ""
        self.read_at: float | None = None
        if self.source() == "argus":
            self.rt.apply_text(self.settings.get(TEXT) or STARTER)

    def source(self) -> str:
        default = "file" if self.rt.config_path.exists() else "argus"
        return self.settings.get(SOURCE) or default

    def text(self) -> str:
        src = self.source()
        if src == "file":
            try:
                return self.rt.config_path.read_text()
            except OSError:
                return ""
        if src == "argus":
            return self.settings.get(TEXT) or STARTER
        return self.rt.config_text or ""

    def view(self) -> dict:
        src = self.source()
        text = self.text()
        return {"source": src, "editable": src != "file", "text": text, "base": digest(text),
                "repo": self.settings.get(REPO, ""), "path": self.settings.get(PATH, "argus.yml"),
                "branch": self.settings.get(BRANCH, ""), "mode": self.settings.get(MODE, "commit"),
                "error": self.error, "read_at": self.read_at,
                "write_token_set": self.settings.has_secret(WRITE_TOKEN),
                "file": str(self.rt.config_path)}

    # --- reading git ---

    async def _fetch(self) -> tuple[str, str] | None:
        repo, path = self.settings.get(REPO), self.settings.get(PATH, "argus.yml")
        if not repo or not self.poller.connected:
            raise EditError("connect GitHub and choose the config repo first", 409)
        params = {"ref": self.settings.get(BRANCH)} if self.settings.get(BRANCH) else {}
        f = await self.poller._get(f"/repos/{repo}/contents/{path}", **params)
        if f is None:
            return None
        return base64.b64decode(f["content"]).decode(), f["sha"]

    async def refresh(self) -> None:
        if self.source() != "github":
            return
        try:
            got = await self._fetch()
        except (EditError, httpx.HTTPError) as e:
            self.error = str(e)
            return
        if got is None:
            where = self.settings.get(PATH, "argus.yml")
            self.error = f"{where} is not in {self.settings.get(REPO)}"
            return
        text, sha = got
        self.read_at = self.clock()
        if sha == self.sha:
            return
        try:
            self.rt.apply_text(text)
            self.sha, self.error = sha, ""
        except Exception as e:
            # Keep the running config; say why the newest one was not taken.
            self.sha, self.error = sha, f"the newest {self.settings.get(PATH)} was rejected: {e}"
            self.rt.reload_ok = False

    async def run_forever(self, every: float = 60) -> None:
        import asyncio
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("config refresh crashed")
            await asyncio.sleep(every)

    # --- saving ---

    async def save(self, text: str, base: str, why: str) -> dict:
        """Save a whole new text that was made from version `base`."""
        src = self.source()
        if src == "file":
            raise EditError("this config is a mounted file; edit it where it lives", 409)
        if digest(self.text()) != base:
            raise EditError("the config changed since you opened it; here is the newer one", 409)
        validate(text)
        if src == "argus":
            self.settings.set(TEXT, text)
            self.rt.apply_text(text)
            return {"saved": "argus"}
        return await self._commit(text, why)

    async def _commit(self, text: str, why: str) -> dict:
        token = self.settings.secret(WRITE_TOKEN)
        if not token:
            raise EditError("saving to git needs the write token (GitHub page, Deploys)", 409)
        repo, path = self.settings.get(REPO), self.settings.get(PATH, "argus.yml")
        c, h = self.poller.client, {"Authorization": f"Bearer {token}"}

        async def call(method: str, url: str, **kw) -> Any:
            r = await c.request(method, url, headers=h, auth=None, **kw)
            if r.status_code in (409, 422) and "contents" in url:
                raise EditError("the file changed in git since Argus read it; reload and redo "
                                "the edit", 409)
            r.raise_for_status()
            return r.json()

        info = await call("GET", f"/repos/{repo}")
        branch = self.settings.get(BRANCH) or info["default_branch"]
        body = {"message": f"argus.yml: {why}\n\nEdited in Argus.", "sha": self.sha,
                "content": base64.b64encode(text.encode()).decode()}
        if self.settings.get(MODE, "commit") == "commit":
            out = await call("PUT", f"/repos/{repo}/contents/{path}",
                             json={**body, "branch": branch})
            self.sha = out["content"]["sha"]
            self.rt.apply_text(text)
            return {"saved": "commit", "url": out["commit"]["html_url"]}
        head = await call("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        new = f"argus/edit-{int(self.clock())}"
        await call("POST", f"/repos/{repo}/git/refs",
                   json={"ref": f"refs/heads/{new}", "sha": head["object"]["sha"]})
        await call("PUT", f"/repos/{repo}/contents/{path}", json={**body, "branch": new})
        pr = await call("POST", f"/repos/{repo}/pulls", json={
            "title": f"argus.yml: {why}", "head": new, "base": branch,
            "body": "Edited in Argus. It takes effect when this is merged."})
        return {"saved": "pr", "url": pr["html_url"]}


def import_preview(text: str, current: str) -> list[dict]:
    """The checks in a pasted argus.yml, each marked new, already here (by name
    or by what it probes) or unusable, with the reason. Sites this config does
    not have are dropped rather than refused."""
    try:
        doc = _yaml().load(text) or {}
    except Exception as e:  # yaml errors, shown to the owner
        raise EditError(f"that is not YAML Argus can read: {e}") from None
    raw = doc.get("checks") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        raise EditError("no checks: list in that text")
    here = validate(current) if current.strip() else cfg.Config()
    names = {c.name for c in here.checks}
    # Covered: the same port is watched already, by any kind or path.
    watched = {e: c.name for c in here.checks if (e := cfg.endpoint(c))}
    targets = {(c.kind, c.target.rstrip("/")): c.name for c in here.checks}
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        entry = {k: v for k, v in dict(e).items()}
        if entry.get("site") and entry["site"] not in here.sites:
            entry.pop("site")
        try:
            c = cfg.Check.model_validate(entry)
        except Exception as err:  # pydantic: say which field
            out.append({"name": str(entry.get("name", "?")), "state": "invalid",
                        "why": str(err).splitlines()[0][:200], "entry": None})
            continue
        by = targets.get((c.kind, c.target.rstrip("/"))) or watched.get(cfg.endpoint(c))
        state = "exists" if c.name in names else "covered" if by else "new"
        out.append({"name": c.name, "state": state, "kind": c.kind, "target": c.target,
                    **({"covered_by": by} if state == "covered" else {}),
                    "group": c.group, "severity": c.severity,
                    "entry": c.model_dump(mode="json", exclude_defaults=True)})
    return out


def import_checks(current: str, entries: list[dict]) -> str:
    """The config with these checks added, each after its group."""
    text = current
    for e in entries:
        text = upsert(text, "checks", e)
    return text
