"""argus.yml: the checks, heartbeats and jobs Argus watches. Lives in homelab-ops.

Validation is strict on purpose: unknown keys, duplicate names and malformed
targets are errors, so a typo cannot silently drop a check. A config that fails
validation is never applied; the running one stays (see Runtime.maybe_reload).
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME = r"^[a-z0-9][a-z0-9-]{0,62}$"
REPO = r"^[A-Za-z0-9._-]{1,100}$"
Severity = Literal["critical", "warning"]
# A site is a name and an order: one per network or place ("home", "office").
SITE = r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$"
# Empty means the first site in `sites` (Config fills it in).
Site = Annotated[str, Field(pattern=r"^$|" + SITE)]
SiteId = Annotated[str, Field(pattern=SITE)]
SiteName = Annotated[str, Field(min_length=1, max_length=64)]


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME)
    kind: Literal["http", "tcp", "ping"]
    target: str
    group: str = "default"
    site: Site = ""
    severity: Severity = "warning"
    interval: int = Field(60, ge=10, le=3600)
    timeout: float = Field(5, gt=0, le=60)
    expect_status: tuple[int, ...] = (200,)
    verify_tls: bool = True
    description: str = ""
    # Run from inside this host by its companion, for what Argus cannot reach.
    companion: str = Field("", pattern=r"^$|" + NAME)

    @model_validator(mode="after")
    def _shape(self) -> Check:
        if self.timeout >= self.interval:
            raise ValueError(f"{self.name}: timeout must be shorter than interval")
        if self.kind == "http":
            parts = urlsplit(self.target)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"{self.name}: http target must be an http(s) URL")
        elif self.kind == "tcp":
            host, sep, port = self.target.rpartition(":")
            if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError(f"{self.name}: tcp target must be host:port")
        elif any(c in self.target for c in "/: "):
            raise ValueError(f"{self.name}: ping target must be a bare host")
        return self

    @property
    def host(self) -> str:
        if self.kind == "http":
            return urlsplit(self.target).hostname or ""
        if self.kind == "tcp":
            return self.target.rpartition(":")[0].strip("[]")
        return self.target


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME)
    max_age: int = Field(ge=60, description="seconds without a beat before it counts as late")
    group: str = "default"
    site: Site = ""
    severity: Severity = "warning"
    description: str = ""


class Job(BaseModel):
    """A scheduled run that reports its result here (decision 0017).

    systemd on ops-runtime runs it (a playbook, say) and POSTs the outcome to
    /job/<name>. Argus never starts anything itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME)
    max_age: int = Field(ge=60, description="seconds without a run before it counts as late")
    # When it runs, for people (the timer is the source of truth), e.g. "Mondays 03:00".
    schedule: str = ""
    group: str = "default"
    site: Site = ""
    severity: Severity = "warning"
    description: str = ""
    # For people who do not know the setup: what it covers, in a few words
    # ("Every VM and container"), and the steps it takes, in order.
    title: str = ""
    steps: tuple[str, ...] = ()


class Link(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)
    url: str
    group: str = "Links"

    @model_validator(mode="after")
    def _url(self) -> Link:
        if urlsplit(self.url).scheme not in ("http", "https"):
            raise ValueError(f"link {self.label}: url must be http(s)")
        return self


class GitHub(BaseModel):
    """Where the app pipeline lives, for the Updates view and each tile's track.

    Argus only reads: the token on ops-runtime is read-only (decision 0010).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str = Field(pattern=r"^[A-Za-z0-9-]{1,39}$")
    # The repo with sites/<site>/<stack>/compose.yml and hosts/<hostname>.
    stacks_repo: str = Field("homelab-stacks", pattern=REPO)
    poll: int = Field(300, ge=60, le=3600, description="seconds between polls")


class Repo(BaseModel):
    """One repo the pipeline patches: what it deploys as, and which check sees it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # owner/name also works, to tell apart two owners' repos of one name.
    name: str = Field(pattern=r"^([A-Za-z0-9-]{1,39}/)?[A-Za-z0-9._-]{1,100}$")
    # Its folder in the stacks repo, when stack-sync deploys it.
    stack: str = Field("", pattern=r"^$|^sites/[a-z0-9-]+/[a-z0-9-]+$")
    # The check that shows it is up; its tile gets this repo's track.
    check: str = ""
    # When stack-sync does not deploy it: the heartbeat of the timer that does
    # (mars-sync). With neither, the repo is not deployed on its own (a library).
    deploy: str = ""


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Display names for the sites, in display order. Names live in the config,
    # never in the code. Left out, the sites are the ones the entries name, in
    # order of first use, each called by its id ("home" when none names one).
    sites: dict[SiteId, SiteName] = {}
    # Footer links in the UI (dashboards, alert pages), grouped by `group`.
    links: tuple[Link, ...] = ()
    # Hosts no check may touch, by IP: someone else's machines on a shared
    # network. A standing rule that is cheaper to enforce here than in review.
    never_probe: tuple[str, ...] = ()
    # Optional one-line description per group, in display order; the UI builds
    # its summary from these. A group a check uses but this omits still shows.
    groups: dict[str, str] = {}
    checks: tuple[Check, ...] = ()
    heartbeats: tuple[Heartbeat, ...] = ()
    jobs: tuple[Job, ...] = ()
    # A short paragraph above the jobs in the UI, for people new to the setup.
    jobs_intro: str = ""
    # The app pipeline: optional; without it the Updates view and tracks are off.
    github: GitHub | None = None
    repos: tuple[Repo, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _sites(cls, data: Any) -> Any:
        """Give every entry a site, so one-site setups never mention sites at all."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        kinds = ("checks", "heartbeats", "jobs")
        if not data.get("sites"):
            used = [e.get("site") or "home" for k in kinds for e in data.get(k) or ()
                    if isinstance(e, dict)]
            data["sites"] = {s: s for s in dict.fromkeys(used or ["home"])}
        if not isinstance(data["sites"], dict):
            return data
        first = next(iter(data["sites"]))
        for k in kinds:
            if isinstance(data.get(k), list):
                data[k] = [{**e, "site": e.get("site") or first} if isinstance(e, dict) else e
                           for e in data[k]]
        return data

    @model_validator(mode="after")
    def _rules(self) -> Config:
        for e in (*self.checks, *self.heartbeats, *self.jobs):
            if e.site not in self.sites:
                known = ", ".join(self.sites)
                raise ValueError(f"{e.name}: unknown site {e.site} (sites: {known})")
        names = ([c.name for c in self.checks] + [h.name for h in self.heartbeats]
                 + [j.name for j in self.jobs])
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate names: {', '.join(dupes)}")
        unknown = sorted(set(self.groups) - {c.group for c in self.checks})
        if unknown:
            raise ValueError(f"groups with no checks: {', '.join(unknown)}")
        # Without a github section, repos only annotate the ones "Connect GitHub"
        # finds (a check, say); stacks need the section's deploy repo.
        if any(r.stack for r in self.repos) and self.github is None:
            raise ValueError("repos with a stack need a github section")
        repo_names = [r.name for r in self.repos]
        dupes = sorted({n for n in repo_names if repo_names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate repos: {', '.join(dupes)}")
        check_names = {c.name for c in self.checks}
        linked = [r.check for r in self.repos if r.check]
        for r in self.repos:
            if r.check and r.check not in check_names:
                raise ValueError(f"repo {r.name}: no check named {r.check}")
        twice = sorted({n for n in linked if linked.count(n) > 1})
        if twice:
            raise ValueError(f"checks linked to more than one repo: {', '.join(twice)}")
        banned = {str(ipaddress.ip_address(h)) for h in self.never_probe}
        for c in self.checks:
            if _resolve(c.host) & banned:
                raise ValueError(f"{c.name}: {c.host} is in never_probe")
        return self


def _resolve(host: str) -> set[str]:
    """Every address a host name can mean, so a DNS name cannot slip past never_probe."""
    try:
        return {str(ipaddress.ip_address(host))}
    except ValueError:
        pass
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return set()


def parse(text: str) -> Config:
    """Validate a config text (from the file, the database or git)."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("the config must be a mapping (checks:, heartbeats:, ...)")
    return Config.model_validate(data)


def load(path: Path) -> tuple[Config, str]:
    """Parse and validate. Returns the config and a short hash of the file."""
    text = path.read_text()
    return parse(text), hashlib.sha256(text.encode()).hexdigest()[:12]


def endpoint(check: Check) -> tuple[str, int] | None:
    """The (host, port) a check knocks on, whatever its kind or path, so two
    checks of one port are seen as one (an HTTP /_up and a TCP check of :5984).
    A check run from inside a host is keyed by that host. None for ping."""
    if check.kind == "tcp":
        host, _, port = check.target.rpartition(":")
        key = (host.strip("[]"), int(port))
    elif check.kind == "http":
        u = urlsplit(check.target)
        key = (u.hostname or "", u.port or (443 if u.scheme == "https" else 80))
    else:
        return None
    if check.companion and key[0] in ("127.0.0.1", "localhost", "::1"):
        return (f"companion:{check.companion}", key[1])
    return key
