"""HTTP surface.

- Open: the dashboard's reads (unless "require login to view" is on), /metrics,
  /healthz, and the two writes timers make (heartbeats and job reports).
- Behind the admin login: settings, and the actions on the Alerts page. Every
  such write needs the session's CSRF token in X-CSRF-Token.
- Companions (under /companion/, outside /api/): enrollment with a single-use
  code, then reports with the companion's own token. Replies are signed.

Handlers are async on purpose: they then run on the event loop thread, the same
one the scheduler writes from, so SQLite needs no locking.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from argus import backup, configsrc, ghauth, github, leave, pipeline, start
from argus.adopt import Adoption
from argus.alerts import (
    ALERTMANAGER_EXAMPLE,
    TOKEN,
    WATCH_URL,
    Alerter,
    SendError,
    prometheus_rules,
    recent_chats,
)
from argus.auth import COOKIE, SESSION_DAYS, Auth, AuthError, Session
from argus.companions import REPORT_EVERY, EnrollError, Signer, name_from
from argus.deploys import BRANCH, DEPENDABOT, ENV_KEY, PULL_TOKEN, STACK, WRITE_TOKEN, Deploys
from argus.guests import GIVE_UP, GuestInstalls
from argus.metrics import registry
from argus.patching import REQUEST, Patching
from argus.probes import Result
from argus.runtime import Runtime
from argus.selfupdate import SelfUpdate
from argus.settings import Settings, load_key
from argus.sniff import Sniffer
from argus.uninstalls import COMMAND as UNINSTALL_COMMAND
from argus.uninstalls import Uninstalls

# While Argus updates itself, watchers expect silence this long from the request.
WATCH_QUIET = 15 * 60
STALL = 30  # seconds without a scheduler tick before /healthz fails
MAX_REPORT = 256 * 1024  # bytes in a job report
LOG_TAIL = 64 * 1024  # characters of log kept per run
# The read-only GitHub token (decision 0010). Absent: no Updates view, no tracks.
TOKEN_FILE = Path(os.environ.get("ARGUS_GITHUB_TOKEN_FILE", "/run/secrets/github_token"))
UI_DIR = Path(os.environ.get("ARGUS_UI", Path(__file__).resolve().parents[2] / "ui" / "dist"))
# The companion builds (linux-amd64, linux-arm64), put there by the image build.
COMPANION_DIR = Path(os.environ.get("ARGUS_COMPANION_DIR", "/app/companion"))
_BUILDS: dict[str, tuple[float, str]] = {}


STALE_COMPANION = 3 * 3600  # self-update tries hourly: three hours behind is stuck


def companion_sha256(arch: str) -> str:
    """The sha256 of the companion build served for a CPU, "" when there is none.
    Companions compare it with their own and update themselves when it differs."""
    if arch not in ("amd64", "arm64"):
        return ""
    path = COMPANION_DIR / f"argus-companion-linux-{arch}"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    if arch not in _BUILDS or _BUILDS[arch][0] != mtime:
        _BUILDS[arch] = (mtime, hashlib.sha256(path.read_bytes()).hexdigest())
    return _BUILDS[arch][1]

INSTALL_SH = Path(__file__).with_name("companion-install.sh")
MAX_COMPANION_REPORT = 512 * 1024  # bytes

# The UI is same-origin only: no CDNs, no external fonts, no inline scripts.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action https://github.com",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class RunItem(BaseModel):
    """One thing a run covered (a guest, a host, a playbook) and how it went."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    result: Literal["ok", "failed", "skipped", "pending"]
    detail: str = Field("", max_length=300)


class JobRun(BaseModel):
    """What a scheduled run reports when it ends (decision 0017)."""

    model_config = ConfigDict(extra="forbid")

    result: Literal["ok", "failed"]
    started: float = Field(gt=0)
    finished: float = Field(gt=0)
    summary: str = Field("", max_length=500)
    items: list[RunItem] = Field([], max_length=200)
    # The tail is what matters (where it failed); longer logs are cut from the front.
    log: str = ""

    @model_validator(mode="after")
    def _order(self) -> JobRun:
        if self.finished < self.started:
            raise ValueError("finished is before started")
        self.log = self.log[-LOG_TAIL:]
        return self


class Password(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=1024)


class Setup(Password):
    code: str = Field(max_length=32)


class Pause(BaseModel):
    """Pause a check: it keeps running but shows grey and never pages."""
    model_config = ConfigDict(extra="forbid")
    reason: str = Field("", max_length=200)
    until: Literal["up", "resumed"] = "up"


class NewPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


def _http_url(v: str | None) -> str | None:
    if v and (urlsplit(v).scheme not in ("http", "https") or not urlsplit(v).hostname):
        raise ValueError("must be an http(s) URL")
    return v


class AlertSettings(BaseModel):
    """A change on the Alerts page. Absent fields stay as they are; an empty secret
    clears it. Secrets are write-only."""

    model_config = ConfigDict(extra="forbid")
    telegram_token: str | None = Field(None, max_length=200, pattern=r"^$|^\d{3,20}:[\w-]{20,100}$")
    chat_id: str | None = Field(None, pattern=r"^$|^-?\d{1,20}$|^@[A-Za-z0-9_]{5,32}$")
    rules: dict[Literal["checks", "jobs", "heartbeats", "companions"], bool] | None = None
    repeat_minutes: int | None = Field(None, ge=0, le=7 * 24 * 60)
    link: str | None = Field(None, max_length=300)
    watch_url: str | None = Field(None, max_length=500)
    # What else watches Argus, in the owner's words ("Prometheus at site A"); "" clears it.
    watch_elsewhere: str | None = Field(None, max_length=120)

    _urls = field_validator("link", "watch_url")(_http_url)


class PipelineImage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    image: str = Field("", max_length=200, pattern=r"^$|^ghcr\.io/[a-z0-9._/-]{1,180}$")
    test_command: str | None = Field(None, max_length=300)


class PipelineEdits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    images: list[PipelineImage] = Field([], max_length=20)


class Switch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: Literal["argus", "prometheus"]


class Access(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view_requires_login: bool


class NewHost(BaseModel):
    """"Add a host": the address the host reaches Argus at, and options."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=300)
    name: str = Field("", pattern=r"^$|^[a-z0-9][a-z0-9-]{0,62}$")
    read_only: bool = False
    hosts: int = Field(1, ge=1, le=50)  # one command for this many hosts


    _url = field_validator("url")(_http_url)

class HostName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


class GuestInstall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=300, pattern=r"^https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~/-]*)?$")


class HostRename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    new: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")  # None: remove it


class ImportChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=200_000)
    names: list[str] | None = Field(None, max_length=500)  # None: preview only
    base: str = ""


class AddChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: list[dict] = Field(min_length=1, max_length=200)
    base: str = ""


class Ignore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container: str = Field(min_length=1, max_length=255)
    ignored: bool


class Enroll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=40)
    hostname: str = Field(max_length=253)


# What a companion reports. Unknown fields are ignored, so an older Argus still
# takes reports from a newer companion.
class Container(BaseModel):
    name: str = Field(max_length=200)
    image: str = Field("", max_length=300)
    state: str = Field("", max_length=40)  # running, exited, restarting, ...
    health: str = Field("", max_length=40)  # healthy, unhealthy, starting, or none
    status: str = Field("", max_length=200)  # Docker's words: "Up 3 hours (healthy)"
    project: str = Field("", max_length=200)  # the compose project, if any
    ports: list[int] = Field([], max_length=100)  # published TCP ports
    local_ports: list[int] = Field([], max_length=100)  # of those, on loopback only
    working_dir: str = Field("", max_length=500)  # its Compose project's folder
    config_files: list[str] = Field([], max_length=20)


class Disk(BaseModel):
    mount: str = Field(max_length=300)
    total: int = Field(ge=0)
    used: int = Field(ge=0)


class Package(BaseModel):
    """One pending update, and for a security one why, from the distro's sources."""
    name: str = Field(max_length=200)
    to: str = Field(max_length=200)
    from_: str = Field("", max_length=200, alias="from")
    security: bool = False
    source: str = Field("", max_length=200)
    advisories: list[str] = Field([], max_length=20)
    cves: list[str] = Field([], max_length=50)
    severity: str = Field("", max_length=20)
    why: Literal["", "none", "unavailable"] = ""
    changes: str = Field("", max_length=500)

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class Updates(BaseModel):
    pending: int | None = Field(None, ge=0)
    security: int | None = Field(None, ge=0)
    packages: list[Package] = Field([], max_length=300)
    manager: str = Field("", max_length=20)
    checked_at: float | None = None
    error: str = Field("", max_length=300)


class DeployResult(BaseModel):
    """How the companion's newest attempt at one stack went."""

    path: str = Field(max_length=300)
    digest: str = Field("", max_length=64)
    result: Literal["ok", "failed", "refused", "rolled_back"]
    detail: str = Field("", max_length=2000)
    at: float | None = None


class RemoteResult(BaseModel):
    name: str = Field(max_length=64)
    ok: bool
    duration: float = Field(ge=0)
    error: str = Field("", max_length=300)


class CollectedProject(BaseModel):
    """A project's files, read by the companion for adoption. Never stored."""

    project: str = Field(max_length=100)
    dir: str = Field("", max_length=500)
    files: dict[str, str] = Field({}, max_length=20)
    env: str = Field("", max_length=64 * 1024)
    error: str = Field("", max_length=500)


class LocalTime(BaseModel):
    """The host's own clock: windows are in its local time."""

    weekday: int = Field(ge=0, le=6)  # Monday is 0
    time: str = Field(pattern=r"^\d\d:\d\d$")
    date: str = Field(pattern=r"^\d{4}-\d\d-\d\d$")
    zone: str = Field("", max_length=64)


class PatchState(BaseModel):
    run: int = Field(ge=0)
    state: Literal["running", "rebooting", "ok", "failed"]
    summary: str = Field("", max_length=500)
    log: str = Field("", max_length=64 * 1024)
    at: float | None = None


class PatchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    day: int = Field(ge=0, le=6)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    level: Literal["security", "full"] = "full"
    reboot: bool = True
    # A job or heartbeat, or a guest's vzdump backup seen by a Proxmox host: pve:<host>/<vmid>.
    backup: str = Field("", pattern=r"^$|^(job|heartbeat):[a-z0-9][a-z0-9-]{0,62}$"
                                    r"|^pve:[a-z0-9][a-z0-9-]{0,62}/[0-9]{1,9}$|^pve:guests$")
    backup_days: int = Field(8, ge=1, le=366)  # how recent the backup must be
    restore: str = Field("", pattern=r"^$|^(job|heartbeat):[a-z0-9][a-z0-9-]{0,62}$")
    elsewhere: bool = False
    elsewhere_job: str = Field("", pattern=r"^$|^(job|heartbeat):[a-z0-9][a-z0-9-]{0,62}$")
    no_backup: bool = False  # the owner can rebuild it: patch without a backup
    outside_backup: str = Field("", max_length=200)  # backed up where Argus cannot see


class PveBackup(BaseModel):
    at: float = Field(gt=0)
    storage: str = Field("", max_length=100)
    size: int = Field(0, ge=0)


class PveGuest(BaseModel):
    vmid: int = Field(ge=1, le=999_999_999)
    name: str = Field("", max_length=100)
    type: Literal["qemu", "lxc"]
    status: str = Field("", max_length=20)
    backup: PveBackup | None = None


class Proxmox(BaseModel):
    """A Proxmox server's guests and the newest vzdump backup of each."""

    node: str = Field("", max_length=100)
    guests: list[PveGuest] = Field([], max_length=2000)
    error: str = Field("", max_length=300)


class GuestResult(BaseModel):
    vmid: int = Field(ge=1, le=999_999_999)
    code: str = Field(max_length=40)
    ok: bool
    output: str = Field("", max_length=8000)


class UninstallResult(BaseModel):
    """How a companion handed its stacks back, sent before it removes itself."""
    state: Literal["ok", "failed"]
    log: str = Field("", max_length=8000)


class Confirm(BaseModel):
    """The admin password twice, for uninstalling a companion (decision 18)."""
    password: str = Field(max_length=1024)
    again: str = Field(max_length=1024)


class ArgusUpdateResult(BaseModel):
    id: str = Field(max_length=40)
    state: Literal["running", "ok", "failed", "rolled_back"]
    summary: str = Field("", max_length=2000)
    log: str = Field("", max_length=32000)


class WatchState(BaseModel):
    """A host that watches Argus with its own bot (decision 19). Never the token."""

    bot: str = Field("", max_length=64)
    bot_id: str = Field("", max_length=24)
    checked_at: float | None = None
    error: str = Field("", max_length=300)
    test: int = Field(0, ge=0)
    test_error: str = Field("", max_length=300)


class Report(BaseModel):
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")
    version: str = Field("", max_length=100)
    hostname: str = Field("", max_length=253)
    os: str = Field("", max_length=120)
    kernel: str = Field("", max_length=120)
    arch: str = Field("", max_length=20)
    uptime: float | None = None
    read_only: bool = False
    containers: list[Container] | None = Field(None, max_length=1000)
    docker_error: str = Field("", max_length=300)
    failed_units: list[str] | None = Field(None, max_length=200)
    disks: list[Disk] = Field([], max_length=100)
    updates: Updates | None = None
    reboot_required: bool | None = None
    checks: list[RemoteResult] = Field([], max_length=1000)
    deploys: list[DeployResult] = Field([], max_length=200)
    local: LocalTime | None = None
    snapshot: str = Field("", max_length=20)  # "zfs" when the root can be snapshotted
    patch: PatchState | None = None
    policy_roots: list[str] = Field([], max_length=50)
    collected: list[CollectedProject] = Field([], max_length=20)
    proxmox: Proxmox | None = None
    guest_installs: list[GuestResult] = Field([], max_length=50)
    argus_update: ArgusUpdateResult | None = None
    uninstall: UninstallResult | None = None
    watch: WatchState | None = None


class DeploySettings(BaseModel):
    """Where deploys come from. Absent fields stay; an empty pull token clears it."""

    model_config = ConfigDict(extra="forbid")
    repo: str | None = Field(None, pattern=r"^$|^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
    branch: str | None = Field(None, max_length=100, pattern=r"^$|^[A-Za-z0-9._/-]+$")
    pull_token: str | None = Field(None, max_length=255,
                                   pattern=r"^$|^(ghp_|github_pat_)[A-Za-z0-9_]{20,250}$")
    write_token: str | None = Field(None, max_length=255,
                                    pattern=r"^$|^github_pat_[A-Za-z0-9_]{20,250}$")


class ConfigSourceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["argus", "github", "file"]
    repo: str = Field("", pattern=r"^$|^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
    path: str = Field("argus.yml", max_length=300, pattern=r"^[A-Za-z0-9._/-]+\.ya?ml$")
    branch: str = Field("", max_length=100, pattern=r"^$|^[A-Za-z0-9._/-]+$")
    mode: Literal["commit", "pr"] = "commit"


class ConfigText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=512 * 1024)
    base: str = Field(max_length=64)


class ConfigEntry(BaseModel):
    """One check, heartbeat or job, added or replacing the one named `replace`."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["checks", "heartbeats", "jobs"] = "checks"
    entry: dict = Field(max_length=40)
    replace: str | None = Field(None, max_length=64)
    base: str = Field(max_length=64)


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=1024)
    passphrase: str = Field(min_length=12, max_length=1024)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: str = Field(max_length=8 * 1024 * 1024)
    passphrase: str = Field(max_length=1024)


class StartHidden(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hidden: bool


class StackSecrets(BaseModel):
    """Changes to one stack's .env: a value sets a key, null removes it."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(max_length=300, pattern=STACK.pattern)
    env: dict[str, str | None] = Field(max_length=200)

    @field_validator("env")
    @classmethod
    def _keys(cls, v: dict[str, str | None]) -> dict[str, str | None]:
        for k, val in v.items():
            if not ENV_KEY.match(k):
                raise ValueError(f"{k} is not a valid variable name")
            if val is not None and len(val) > 8192:
                raise ValueError(f"{k} is too long")
        return v


def session_of(request: Request) -> Session | None:
    return request.app.state.auth.session(request.cookies.get(COOKIE))


def admin(request: Request) -> Session:
    """The admin's session, or 401. (The view_gate middleware has already checked
    the CSRF token of every write.)"""
    s = session_of(request)
    if s is None:
        raise HTTPException(401, "sign in first")
    return s


# At module level: annotations are strings here, resolved in module globals.
Admin = Annotated[Session, Depends(admin)]

# Reachable without a session even when viewing needs one.
OPEN_API = {"/api/session", "/api/setup", "/api/login"}


class GitHubSettings(BaseModel):
    """A change on the GitHub page. An empty token clears it."""

    model_config = ConfigDict(extra="forbid")
    token: str | None = Field(None, max_length=255,
                              pattern=r"^$|^(github_pat_|ghp_)[A-Za-z0-9_]{20,250}$")
    hidden: list[str] | None = Field(None, max_length=2000)


class NewApp(BaseModel):
    """"Connect GitHub": Argus's address as the browser sees it, and where the app lives."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=300)
    # GitHub app names are unique across GitHub and at most 34 characters.
    name: str = Field(min_length=3, max_length=34, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    org: str = Field("", pattern=r"^$|^[A-Za-z0-9-]{1,39}$")

    _url = field_validator("url")(_http_url)


def create_app(runtime: Runtime | None = None, start_scheduler: bool = True,
               poller: github.Poller | None = None,
               alert_client: httpx.AsyncClient | None = None,
               tls_pin: str | None = None,
               github_client: httpx.AsyncClient | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or Runtime(
            Path(os.environ.get("ARGUS_CONFIG", "/config/argus.yml")),
            os.environ.get("ARGUS_DB", "/data/argus.db"),
        )
        key = load_key(Path(os.environ.get("ARGUS_KEY", rt.db_path.parent / "argus.key")))
        settings = Settings(rt.store.db, key)
        client = alert_client or httpx.AsyncClient()
        gh_client = github_client or httpx.AsyncClient(timeout=20)
        gh = poller or github.Poller(ghauth.credentials(settings, gh_client, TOKEN_FILE, rt.clock))
        alerter = Alerter(rt, settings, client)
        auth = Auth(rt.store.db, settings, rt.clock)
        auth.setup_code()  # logs it when there is no password yet
        app.state.rt, app.state.gh, app.state.gh_client = rt, gh, gh_client
        app.state.settings, app.state.auth, app.state.alerter = settings, auth, alerter
        app.state.signer, app.state.tls_pin = Signer(settings), tls_pin
        folder = os.environ.get("ARGUS_DEPLOY_DIR")
        deploys = Deploys(settings, gh, rt.clock, Path(folder) if folder else None)
        app.state.deploys = alerter.deploys = deploys
        app.state.configsrc = configsrc.ConfigSource(rt, settings, gh, rt.clock)
        app.state.patching = alerter.patching = Patching(rt.store.db, settings, rt, rt.clock)
        app.state.adoption = Adoption(settings, deploys, rt.clock)
        app.state.guests = GuestInstalls(rt.clock)
        app.state.uninstalls = Uninstalls(settings, rt.clock)
        app.state.sniffer = Sniffer(rt.clock)
        app.state.behind = {}  # host -> when its companion first lagged this build
        app.state.selfupdate = SelfUpdate(settings, client, rt.clock)
        app.state.registry = registry(rt, gh, alerter.prometheus_test_active)
        tasks = []
        if start_scheduler:
            tasks.append(asyncio.create_task(rt.run_forever()))
            tasks.append(asyncio.create_task(alerter.run_forever()))
            tasks.append(asyncio.create_task(deploys.run_forever()))
            tasks.append(asyncio.create_task(app.state.configsrc.run_forever()))
            tasks.append(asyncio.create_task(app.state.selfupdate.run_forever()))
            tasks.append(asyncio.create_task(github.run_forever(
                gh, lambda: rt.config, lambda: set(settings.get(ghauth.HIDDEN, [])))))
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await gh.aclose()
        if github_client is None:
            await gh_client.aclose()
        if alert_client is None:
            await client.aclose()
        await rt.aclose()

    app = FastAPI(title="Argus", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def rt(request: Request) -> Runtime:
        return request.app.state.rt

    def address(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def deny(status: int, detail: str) -> Response:
        # This middleware wraps security_headers, so it adds them itself.
        return Response(json.dumps({"detail": detail}), status,
                        media_type="application/json", headers=SECURITY_HEADERS)

    @app.middleware("http")
    async def view_gate(request: Request, call_next):
        """Every write under /api/ needs the admin's session and CSRF token, and with
        "require login to view" on, so do the reads. /metrics, /healthz and the
        timers' writes stay open."""
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_API:
            s = session_of(request)
            write = request.method not in ("GET", "HEAD")
            # Writes are checked here, before the body is even parsed, so a
            # stranger learns nothing from validation errors.
            if s is None and (write or request.app.state.settings.get(
                    "access.view_requires_login", False)):
                return deny(401, "sign in first")
            if write and not hmac.compare_digest(
                    request.headers.get("x-csrf-token", "").encode(), s.csrf.encode()):
                return deny(403, "missing or wrong CSRF token; reload the page")
        return await call_next(request)

    def signed_in(request: Request, response: Response, token: str) -> dict:
        response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, path="/",
                            httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
        return session_view(request, request.app.state.auth.session(token))

    def session_view(request: Request, s: Session | None) -> dict:
        auth = request.app.state.auth
        return {"setup_needed": auth.needs_setup(), "signed_in": s is not None,
                "csrf": s.csrf if s else None,
                "view_requires_login": request.app.state.settings.get(
                    "access.view_requires_login", False)}

    def auth_call(fn, *args):
        try:
            return fn(*args)
        except AuthError as e:
            raise HTTPException(e.status, str(e)) from None

    @app.get("/api/session")
    async def get_session(request: Request) -> dict:
        request.app.state.auth.setup_code()  # re-logs a code after reset-login
        return session_view(request, session_of(request))

    @app.post("/api/setup")
    async def setup(body: Setup, request: Request, response: Response) -> dict:
        token = auth_call(request.app.state.auth.setup, body.code, body.password,
                          address(request))
        return signed_in(request, response, token)

    @app.post("/api/login")
    async def login(body: Password, request: Request, response: Response) -> dict:
        token = auth_call(request.app.state.auth.login, body.password, address(request))
        return signed_in(request, response, token)

    @app.post("/api/logout")
    async def logout(request: Request, response: Response,
                     _: Admin) -> dict:
        request.app.state.auth.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return session_view(request, None)

    @app.post("/api/password")
    async def password(body: NewPassword, request: Request, response: Response,
                       _: Admin) -> dict:
        token = auth_call(request.app.state.auth.change_password,
                          request.cookies.get(COOKIE), body.current, body.new, address(request))
        return signed_in(request, response, token)

    @app.put("/api/access")
    async def access(body: Access, request: Request, _: Admin) -> dict:
        request.app.state.settings.set("access.view_requires_login", body.view_requires_login)
        return session_view(request, session_of(request))

    @app.get("/api/alerts")
    async def alerts(request: Request, _: Admin) -> dict:
        return request.app.state.alerter.view()

    @app.put("/api/alerts")
    async def alert_settings(body: AlertSettings, request: Request,
                             _: Admin) -> dict:
        s, alerter = request.app.state.settings, request.app.state.alerter
        if body.telegram_token is not None:
            s.set_secret(TOKEN, body.telegram_token)
        if body.watch_url is not None:
            s.set_secret(WATCH_URL, body.watch_url)
            s.set("watch.state", None)
        if body.watch_elsewhere is not None:
            s.set("watch.elsewhere", body.watch_elsewhere.strip())
        if body.telegram_token is not None or body.chat_id is not None:
            s.set("alerts.test_ok_at", None)  # a new destination needs a new test
            s.set("alerts.delivery", None)
        for field, key in (("chat_id", "alerts.telegram.chat_id"), ("rules", "alerts.rules"),
                           ("repeat_minutes", "alerts.repeat_minutes"), ("link", "alerts.link")):
            value = getattr(body, field)
            if value is not None:
                s.set(key, value)
        return alerter.view()

    @app.post("/api/alerts/test")
    async def alert_test(request: Request, _: Admin) -> dict:
        error = await request.app.state.alerter.test()
        return {"ok": error is None, "error": error}

    @app.post("/api/alerts/watchers/test")
    async def watchers_test(request: Request, _: Admin) -> dict:
        """Every watcher sends a test through its own bot on its next report."""
        s = request.app.state.settings
        s.set("watch.test", s.get("watch.test", 0) + 1)
        return request.app.state.alerter.view()

    @app.post("/api/alerts/telegram-chats")
    async def telegram_chats(request: Request, _: Admin) -> dict:
        token = request.app.state.settings.secret(TOKEN)
        if not token:
            raise HTTPException(409, "save the bot token first")
        try:
            chats = await recent_chats(token, request.app.state.alerter.client)
        except SendError as e:
            raise HTTPException(502, str(e)) from None
        return {"chats": chats}

    @app.post("/api/alerts/prometheus-test")
    async def prometheus_test(request: Request, _: Admin) -> dict:
        request.app.state.alerter.prometheus_test()
        return request.app.state.alerter.view()

    @app.post("/api/alerts/sender")
    async def switch(body: Switch, request: Request, _: Admin) -> dict:
        why = request.app.state.alerter.switch(body.sender)
        if why:
            raise HTTPException(409, why)
        return request.app.state.alerter.view()

    @app.get("/api/alerts/prometheus-rules.yml")
    async def rules_file(request: Request) -> Response:
        return Response(prometheus_rules(request.app.state.settings.get("alerts.link", "")),
                        media_type="application/yaml", headers={
                            "Content-Disposition": 'attachment; filename="argus-rules.yml"'})

    @app.get("/api/alerts/alertmanager-example.yml")
    async def alertmanager_file() -> Response:
        return Response(ALERTMANAGER_EXAMPLE, media_type="application/yaml", headers={
            "Content-Disposition": 'attachment; filename="alertmanager-example.yml"'})

    # --- GitHub: the connection ---

    def github_view(request: Request) -> dict:
        s, gh = request.app.state.settings, request.app.state.gh
        hidden = set(s.get(ghauth.HIDDEN, []))
        about = gh.creds.describe() if gh.creds else {"source": "none"}
        if about.get("app"):
            about["settings_url"] = ghauth.settings_url(about["app"])
        return {
            **about,
            "connect_error": s.get("github.connect_error"),
            "token_set": s.has_secret(ghauth.TOKEN),
            "file": TOKEN_FILE.is_file(),
            "from_config": bool(rt(request).config.github and rt(request).config.repos),
            "repos": [{**x, "hidden": x["full_name"] in hidden} for x in gh.found or []],
            "found_error": gh.found_error,
            "last_success": gh.last_success, "last_error": gh.shown_error,
            "rate_remaining": gh.rate and gh.rate[0],
        }

    async def reconnect(request: Request) -> None:
        gh = request.app.state.gh
        gh.connect(ghauth.credentials(request.app.state.settings, request.app.state.gh_client,
                                      TOKEN_FILE, rt(request).clock))
        await gh.discover(force=True)

    @app.get("/api/github")
    async def github_settings(request: Request, _: Admin) -> dict:
        return github_view(request)

    @app.put("/api/github")
    async def github_change(body: GitHubSettings, request: Request, _: Admin) -> dict:
        s = request.app.state.settings
        if body.hidden is not None:
            s.set(ghauth.HIDDEN, sorted(set(body.hidden)))
        if body.token is not None:
            if s.get(ghauth.APP):
                raise HTTPException(409, "Argus's GitHub App is connected; disconnect it first")
            s.set_secret(ghauth.TOKEN, body.token or None)
            await reconnect(request)
            if body.token and request.app.state.gh.found_error:
                s.set_secret(ghauth.TOKEN, None)
                await reconnect(request)
                raise HTTPException(400, "GitHub refused that token; check it and its repo access")
        return github_view(request)

    @app.post("/api/github/refresh")
    async def github_refresh(request: Request, _: Admin) -> dict:
        await request.app.state.gh.discover(force=True)
        return github_view(request)

    @app.post("/api/github/manifest")
    async def github_manifest(body: NewApp, request: Request, _: Admin) -> dict:
        request.app.state.settings.set("github.connect_error", None)
        return ghauth.manifest(request.app.state.settings, body.url, body.name, body.org,
                               rt(request).clock)

    # --- The app pipeline (decision 20): one pull request per repo ---

    def pipeline_repo(request: Request, owner: str, repo: str) -> dict:
        gh = request.app.state.gh
        if not gh.connected:
            raise HTTPException(409, "connect GitHub first")
        found = next((x for x in gh.found or [] if x["owner"].lower() == owner.lower()
                      and x["name"].lower() == repo.lower()), None)
        if found is None:
            raise HTTPException(404, f"Argus cannot see {owner}/{repo} on GitHub")
        return found

    async def pipeline_plan(request: Request, r: dict, token: str,
                            edits: list[dict] | None = None) -> pipeline.Plan:
        try:
            got = await pipeline.read_repo(request.app.state.gh_client, token, r["owner"],
                                           r["name"], r["default_branch"])
        except pipeline.GitHubError as e:
            raise HTTPException(502, str(e)) from None
        return pipeline.plan(r["owner"], r["name"], r["default_branch"], got["paths"],
                             got["texts"], got["ci"], got["has_dependabot"], edits)

    @app.get("/api/github/pipeline/{owner}/{repo}")
    async def pipeline_preview(owner: str, repo: str, request: Request, _: Admin) -> dict:
        """What the pull request would add, for the owner to look over first."""
        r = pipeline_repo(request, owner, repo)
        token = await request.app.state.gh.creds.token_for(r["owner"])
        if not token:
            raise HTTPException(409, "the GitHub App is not installed on " + r["owner"])
        return {**(await pipeline_plan(request, r, token)).view(),
                "repo": r["full_name"], "branch": r["default_branch"]}

    @app.post("/api/github/pipeline/{owner}/{repo}")
    async def pipeline_open(owner: str, repo: str, body: PipelineEdits, request: Request,
                            _: Admin) -> dict:
        """Open the pull request. Argus never merges it: the owner does."""
        r = pipeline_repo(request, owner, repo)
        creds = request.app.state.gh.creds
        try:
            token = await creds.write_token(r["owner"], r["name"])
        except ghauth.NeedsPermission as e:
            app_info = creds.describe().get("app") or {}
            raise HTTPException(409, {
                "needs_permission": True, "why": str(e),
                "settings_url": (ghauth.settings_url(app_info) + "/permissions"
                                 if app_info else None),
                "install_url": next((i["url"] for i in creds.describe().get("installations", [])
                                     if i["account"].lower() == r["owner"].lower()), None),
            }) from None
        p = await pipeline_plan(request, r, token, [e.model_dump() for e in body.images])
        if p.blocked:
            raise HTTPException(409, p.blocked)
        try:
            url = await pipeline.open_pr(request.app.state.gh_client, token, r["owner"],
                                         r["name"], r["default_branch"], p.files,
                                         pipeline.pr_body(p))
        except pipeline.GitHubError as e:
            why = str(e)
            if "HTTP 403" in why or "HTTP 404" in why:
                why += (". The token needs Contents, Pull requests and Workflows read and write"
                        " on this repo.")
            raise HTTPException(502, why) from None
        logging.getLogger("argus").info("pipeline pull request for %s: %s", r["full_name"], url)
        return {"url": url}

    @app.post("/api/github/disconnect")
    async def github_disconnect(request: Request, _: Admin) -> dict:
        s = request.app.state.settings
        for secret in (ghauth.APP_KEY, ghauth.TOKEN):
            s.set_secret(secret, None)
        s.set(ghauth.APP, None)
        await reconnect(request)
        return github_view(request)

    # GitHub sends the browser here after the user confirms the new app. Outside
    # /api/: the session cookie (SameSite=strict) does not come along on a
    # redirect from github.com, so the single-use state is what proves it.
    @app.get("/github/manifest-callback")
    async def github_callback(request: Request, code: str = "", state: str = "") -> Response:
        s = request.app.state.settings
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", code):
            s.set("github.connect_error", "GitHub sent no code back")
            return Response(status_code=303, headers={"Location": "/#/github"})
        try:
            app_ = await ghauth.finish(s, request.app.state.gh_client, code, state,
                                       rt(request).clock)
        except (ghauth.ConnectError, httpx.HTTPError) as e:
            s.set("github.connect_error", str(e) if isinstance(e, ghauth.ConnectError)
                  else f"could not reach GitHub ({type(e).__name__})")
            return Response(status_code=303, headers={"Location": "/#/github"})
        s.set("github.connect_error", None)
        request.app.state.gh.connect(ghauth.credentials(
            s, request.app.state.gh_client, TOKEN_FILE, rt(request).clock))
        # Next: install it on the repos to watch. GitHub then sends the browser
        # back to /#/github (the app's setup URL).
        return Response(status_code=303,
                        headers={"Location": f"{app_['html_url']}/installations/new"})

    # --- config: where it lives, and edits from the UI ---

    def edit_call(fn):
        async def run(*args):
            try:
                return await fn(*args)
            except configsrc.EditError as e:
                raise HTTPException(e.status, str(e)) from None
            except httpx.HTTPError as e:
                raise HTTPException(502, f"GitHub said no: {type(e).__name__}") from None
        return run

    @app.get("/api/config")
    async def config_view(request: Request, _: Admin) -> dict:
        return request.app.state.configsrc.view()

    @app.get("/api/config/export.yml")
    async def config_export(request: Request, _: Admin) -> Response:
        return Response(request.app.state.configsrc.text(), media_type="application/yaml",
                        headers={"Content-Disposition": 'attachment; filename="argus.yml"'})

    @app.put("/api/config")
    async def config_save(body: ConfigText, request: Request, _: Admin) -> dict:
        src = request.app.state.configsrc
        out = await edit_call(src.save)(body.text, body.base, "edited as YAML")
        return {**src.view(), **out}

    @app.post("/api/config/entries")
    async def config_entry(body: ConfigEntry, request: Request, _: Admin) -> dict:
        src = request.app.state.configsrc
        name = body.entry.get("name", "")
        try:
            text = configsrc.upsert(src.text(), body.kind, body.entry, body.replace)
        except configsrc.EditError as e:
            raise HTTPException(e.status, str(e)) from None
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        why = f"{'change' if body.replace else 'add'} {body.kind[:-1]} {name}"
        out = await edit_call(src.save)(text, body.base, why)
        return {**src.view(), **out}

    @app.post("/api/config/import")
    async def config_import(body: ImportChecks, request: Request, _: Admin) -> dict:
        """Checks from a pasted argus.yml: a preview, then the chosen new ones added
        in one save."""
        src = request.app.state.configsrc
        try:
            items = configsrc.import_preview(body.text, src.text())
        except configsrc.EditError as e:
            raise HTTPException(e.status, str(e)) from None
        if body.names is None:
            return {"checks": items}
        chosen = [i["entry"] for i in items if i["state"] == "new" and i["name"] in body.names]
        if not chosen:
            raise HTTPException(409, "none of those are new checks")
        try:
            text = configsrc.import_checks(src.text(), chosen)
        except configsrc.EditError as e:
            raise HTTPException(e.status, str(e)) from None
        out = await edit_call(src.save)(text, body.base, f"import {len(chosen)} checks")
        return {**src.view(), **out, "added": len(chosen)}

    @app.post("/api/config/checks")
    async def config_add_checks(body: AddChecks, request: Request, _: Admin) -> dict:
        """Several checks at once (Watch more on the Checks page), in one save."""
        src = request.app.state.configsrc
        try:
            text = configsrc.import_checks(src.text(), body.checks)
        except configsrc.EditError as e:
            raise HTTPException(e.status, str(e)) from None
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        out = await edit_call(src.save)(text, body.base, f"add {len(body.checks)} checks")
        return {**src.view(), **out, "added": len(body.checks)}

    @app.delete("/api/config/entries/{kind}/{name}")
    async def config_remove(kind: Literal["checks", "heartbeats", "jobs"], name: str,
                            base: str, request: Request, _: Admin) -> dict:
        src = request.app.state.configsrc
        try:
            text = configsrc.remove(src.text(), kind, name)
        except configsrc.EditError as e:
            raise HTTPException(e.status, str(e)) from None
        out = await edit_call(src.save)(text, base, f"remove {kind[:-1]} {name}")
        return {**src.view(), **out}

    @app.put("/api/config/source")
    async def config_source(body: ConfigSourceChange, request: Request, _: Admin) -> dict:
        src, s, r = request.app.state.configsrc, request.app.state.settings, rt(request)
        current = src.text()
        if body.source == "file":
            if not r.config_path.exists():
                raise HTTPException(409, f"there is no file at {r.config_path}")
            s.set(configsrc.SOURCE, "file")
            r.watch_file, r.config_mtime = True, None
            r.maybe_reload()
        elif body.source == "argus":
            s.set(configsrc.TEXT, current or configsrc.STARTER)
            s.set(configsrc.SOURCE, "argus")
            r.apply_text(current or configsrc.STARTER)
        else:
            if not body.repo:
                raise HTTPException(422, "choose the repo")
            old = {k: s.get(k) for k in (configsrc.SOURCE, configsrc.REPO, configsrc.PATH,
                                         configsrc.BRANCH, configsrc.MODE)}
            for k, v in ((configsrc.REPO, body.repo), (configsrc.PATH, body.path),
                         (configsrc.BRANCH, body.branch), (configsrc.MODE, body.mode)):
                s.set(k, v)
            try:
                got = await edit_call(src._fetch)()
                if got is None:  # not in the repo yet: put the current config there
                    if not s.secret(configsrc.WRITE_TOKEN):
                        # Most often a wrong path: say so, not "get a token".
                        raise HTTPException(409, f"there is no {body.path} in {body.repo}; check "
                                            "the path, or save the write token (GitHub page, "
                                            "Deploys) to start one there")
                    src.sha = None
                    await edit_call(src._commit)(current or configsrc.STARTER,
                                                 "start Argus's config here")
                s.set(configsrc.SOURCE, "github")
                src.sha = None
                await src.refresh()
            except HTTPException:
                for k, v in old.items():
                    s.set(k, v)
                raise
        return src.view()

    # --- patching ---

    def host_named(request: Request, host: str) -> dict:
        for c in rt(request).companions.views():
            if c["name"] == host:
                return c
        raise HTTPException(404, "no such host")

    @app.put("/api/patching/{host}")
    async def patch_plan(host: str, body: PatchPlan, request: Request, _: Admin) -> dict:
        c = host_named(request, host)
        if body.enabled and body.elsewhere:
            raise HTTPException(422, "a host patched by something else gets no Argus window: "
                                     "two tools patching one host would fight")
        if body.backup == "pve:guests" and not (c["report"] or {}).get("proxmox"):
            raise HTTPException(422, f"{host} is not a Proxmox server: there are no guests"
                                     " whose backups could cover it")
        if body.enabled and not body.backup and not body.no_backup and not body.outside_backup \
                and not (c["report"] or {}).get("snapshot"):
            raise HTTPException(422, "link a backup first: Argus only patches a host it can "
                                     "bring back (or say it needs no backup, or choose "
                                     "Something else patches it)")
        pt = request.app.state.patching
        pt.set_plan(host, body.model_dump())
        return pt.view(host, c["report"])

    @app.post("/api/patching/{host}/now")
    async def patch_now(host: str, request: Request, _: Admin) -> dict:
        c = host_named(request, host)
        if c["read_only"]:
            raise HTTPException(409, f"{host} is monitoring only")
        if request.app.state.patching.plan(host)["elsewhere"]:
            raise HTTPException(409, f"{host} is patched by something else")
        if not request.app.state.patching.safety(host, c["report"])["ok"]:
            raise HTTPException(409, f"{host} has no recent backup, so Argus will not patch it;"
                                     " if it can be rebuilt, say it needs no backup in its"
                                     " patch settings")
        request.app.state.settings.set(REQUEST + host, True)
        return request.app.state.patching.view(host, c["report"])

    # --- Argus's own backup ---

    @app.post("/api/export")
    async def take_export(body: ExportRequest, request: Request, _: Admin) -> Response:
        st = request.app.state
        auth_call(st.auth.confirm, body.password, address(request))
        blob = backup.export(st.settings, rt(request).db_path.parent, body.passphrase)
        st.settings.set(backup.TAKEN, rt(request).clock())
        day = time.strftime("%Y-%m-%d", time.gmtime(rt(request).clock()))
        return Response(blob, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="argus-export-{day}.json"'})

    @app.post("/api/import")
    async def take_import(body: ImportRequest, request: Request, response: Response,
                          _: Admin) -> dict:
        st, r = request.app.state, rt(request)
        try:
            out = backup.restore(st.settings, r.db_path.parent, body.data.encode(),
                                 body.passphrase)
        except backup.BackupError as e:
            raise HTTPException(400, str(e)) from None
        # Carry on as the restored install: its signing key, GitHub, config.
        st.signer = Signer(st.settings)
        st.gh.connect(ghauth.credentials(st.settings, st.gh_client, TOKEN_FILE, r.clock))
        if st.configsrc.source() == "argus":
            r.apply_text(st.settings.get(configsrc.TEXT) or configsrc.STARTER)
        await st.deploys.refresh()
        response.delete_cookie(COOKIE, path="/")
        return {**out, "signed_out": True}

    # --- updating Argus itself, through the companion on its machine ---

    @app.get("/api/argus/version")
    async def argus_version(request: Request) -> dict:
        return request.app.state.selfupdate.view(rt(request).companions.views())

    @app.post("/api/argus/version/check")
    async def argus_version_check(request: Request, _: Admin) -> dict:
        await request.app.state.selfupdate.check()
        return request.app.state.selfupdate.view(rt(request).companions.views())

    @app.post("/api/argus/update")
    async def argus_update(request: Request, _: Admin) -> dict:
        su = request.app.state.selfupdate
        found = su.host_of(rt(request).companions.views())
        if found is None:
            raise HTTPException(409, "no companion runs on Argus's machine, so Argus cannot"
                                     " update itself; use the command shown instead")
        cur = su.state()
        if cur and cur["state"] in ("waiting", "running"):
            raise HTTPException(409, "an update is already on its way")
        su.request(found[0]["name"], found[1])
        return su.view(rt(request).companions.views())

    # --- adopting running stacks, in place ---

    @app.post("/api/adopt/{host}/{project}/read")
    async def adopt_read(host: str, project: str, request: Request, _: Admin) -> dict:
        c = host_named(request, host)
        if c["read_only"]:
            raise HTTPException(409, f"{host} is monitoring only, so Argus does not take its files")
        if not any(p["project"] == project for p in request.app.state.adoption.projects(c)):
            raise HTTPException(404, f"no running Compose project {project} on {host}")
        request.app.state.adoption.request(host, project)
        return request.app.state.adoption.preview(c, project)

    @app.get("/api/adopt/{host}/{project}")
    async def adopt_preview(host: str, project: str, request: Request, _: Admin) -> dict:
        return request.app.state.adoption.preview(host_named(request, host), project)

    @app.post("/api/adopt/{host}/{project}")
    async def adopt_commit(host: str, project: str, request: Request, _: Admin) -> dict:
        try:
            sha = await request.app.state.adoption.adopt(host_named(request, host), project)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        except httpx.HTTPError as e:
            raise HTTPException(502, f"GitHub said no: {type(e).__name__}") from None
        return {"commit": sha}

    # --- getting started ---

    @app.get("/api/start")
    async def get_started(request: Request) -> dict:
        r, st = rt(request), request.app.state
        companions = r.companions.views()
        out = start.view(rt=r, alerts=st.alerter.summary(), gh_connected=st.gh.connected,
                          deploy_views=st.deploys.views(companions), companions=companions,
                          hidden=st.settings.get(start.HIDDEN, False),
                          patched=sum(1 for c in companions if st.patching.counts(c["name"])),
                          exported_at=st.settings.get(backup.TAKEN), sniffer=st.sniffer,
                          seen=set(st.settings.get(start.DONE, [])))
        done = sorted(x["key"] for x in out["steps"] if x["done"])
        if done != sorted(st.settings.get(start.DONE, [])):
            st.settings.set(start.DONE, done)
        return out

    @app.put("/api/start")
    async def hide_start(body: StartHidden, request: Request, _: Admin) -> dict:
        request.app.state.settings.set(start.HIDDEN, body.hidden)
        return await get_started(request)

    # --- deploys ---

    def deploy_view(request: Request) -> dict:
        d, s = request.app.state.deploys, request.app.state.settings
        src = d.source()
        return {"source": src.describe() if src else None,
                "repo": s.get("deploy.repo", ""), "branch": s.get(BRANCH, ""),
                "commit": d.snapshot and d.snapshot.commit, "read_at": d.read_at,
                "error": d.error or d.bump_error, "pull_token_set": s.has_secret(PULL_TOKEN),
                "write_token_set": s.has_secret(WRITE_TOKEN),
                "hosts": d.snapshot.hosts if d.snapshot else {},
                "empty": bool(d.snapshot and d.snapshot.empty),
                "unmatched": d.unmatched(rt(request).companions.views()),
                "connected": request.app.state.gh.connected}

    @app.get("/api/deploys")
    async def deploys_view(request: Request) -> dict:
        return deploy_view(request)

    @app.put("/api/deploys")
    async def deploys_change(body: DeploySettings, request: Request, _: Admin) -> dict:
        s = request.app.state.settings
        if body.repo is not None:
            s.set("deploy.repo", body.repo)
        if body.branch is not None:
            s.set(BRANCH, body.branch)
        if body.pull_token is not None:
            s.set_secret(PULL_TOKEN, body.pull_token or None)
        if body.write_token:
            # It must be able to push to the deploy repo, and is kept only if so.
            repo = s.get("deploy.repo")
            if not repo:
                raise HTTPException(409, "choose the deploy repo first")
            r = await request.app.state.gh.client.get(
                f"/repos/{repo}", headers={"Authorization": f"Bearer {body.write_token}"},
                auth=None)  # this token, not the read connection's
            if r.status_code != 200 or not r.json().get("permissions", {}).get("push"):
                raise HTTPException(400, f"that token cannot push to {repo}; give it "
                                         "Contents: read and write on that repo only")
            s.set_secret(WRITE_TOKEN, body.write_token)
        elif body.write_token == "":
            s.set_secret(WRITE_TOKEN, None)
        await request.app.state.deploys.refresh()
        return deploy_view(request)

    @app.post("/api/deploys/hosts")
    async def deploys_rename_host(body: HostRename, request: Request, _: Admin) -> dict:
        """A hosts/ file under an old name: give it the name Argus knows, or remove it."""
        if body.new and body.new not in rt(request).companions.names():
            raise HTTPException(404, f"no host called {body.new}")
        try:
            await request.app.state.deploys.rename_host(body.old, body.new)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        except httpx.HTTPError as e:
            raise HTTPException(502, f"GitHub said no: {type(e).__name__}") from None
        return deploy_view(request)

    @app.post("/api/deploys/dependabot")
    async def deploys_dependabot(request: Request, _: Admin) -> dict:
        """Dependabot in the deploy repo: a pull request for each newer image."""
        d = request.app.state.deploys
        repo = request.app.state.settings.get("deploy.repo")
        mine = f"/repos/{repo}/contents/.github/dependabot.yml"
        if repo and await request.app.state.gh._get(mine, missing=(404, 409)) is not None:
            # The owner's own config (groups, ignores) is worth more than ours.
            return {"commit": None, "existing": True}
        try:
            sha = await d.commit({".github/dependabot.yml": DEPENDABOT},
                                 "Keep stack images up to date with Dependabot\n\n"
                                 "It opens a pull request for each newer image; Argus deploys "
                                 "it once merged.\n")
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        except httpx.HTTPError as e:
            raise HTTPException(502, f"GitHub said no: {type(e).__name__}") from None
        await d.refresh()
        return {"commit": sha, "existing": False}

    @app.put("/api/deploys/secrets")
    async def stack_secrets(body: StackSecrets, request: Request, _: Admin) -> dict:
        d = request.app.state.deploys
        d.set_env(body.path, body.env)
        return {"path": body.path, "secrets": sorted(d.env(body.path))}

    # --- companions: the UI's side ---

    def install_command(request: Request, url: str, code: str | None = None,
                        read_only: bool = False) -> str:
        """Add a host's command; without a code it only replaces the companion and
        keeps the host's enrollment."""
        url = url.rstrip("/")
        pin = request.app.state.tls_pin if url.startswith("https://") else None
        q = shlex.quote
        fetch = f"curl -fsS -k --pinnedpubkey {q('sha256//' + pin)}" if pin else "curl -fsS"
        args = f"--url {q(url)}" + (f" --pin {q(pin)}" if pin else "")
        if code:
            args += f" --code {code}"
        if read_only:
            args += " --read-only"
        return f"{fetch} {q(url + '/companion/install.sh')} | sudo sh -s -- {args}"

    def companion_stale(request: Request, v: dict) -> bool:
        """A companion that has not caught up with this Argus's build for hours: its
        self-update is failing (or it is one from before self-update), and only the
        install command run on the host helps. Argus says so on the host's page."""
        mine = os.environ.get("ARGUS_COMMIT", "")[:12]
        version = ((v["report"] or {}).get("version") or "")
        behind = request.app.state.behind
        if not mine or mine == "unknown" or v["status"] != "ok" or version.endswith("+" + mine):
            behind.pop(v["name"], None)
            return False
        since = behind.setdefault(v["name"], rt(request).clock())
        return rt(request).clock() - since > STALE_COMPANION

    @app.get("/api/companions")
    async def companions(request: Request) -> dict:
        r = rt(request)
        views = r.companions.views()
        stacks = {d["companion"]: d["stacks"]
                  for d in request.app.state.deploys.views(views)}
        backups = request.app.state.patching.pve_backups(views)
        for v in views:
            v["checks"] = [check_view(r, c) for c in r.companion_checks(v["name"])]
            v["stacks"] = stacks.get(v["name"], [])
            v["patching"] = request.app.state.patching.view(v["name"], v["report"], backups)
            v["adoptable"] = [] if v["read_only"] else request.app.state.adoption.projects(v)
            v["guest_installs"] = request.app.state.guests.view(v["name"])
            v["companion_stale"] = companion_stale(request, v)
            v["uninstall"] = request.app.state.uninstalls.view(v["id"])
        named = {c.companion for c in r.config.checks if c.companion}
        return {"companions": views, "backups": backups,
                "uninstalled": request.app.state.uninstalls.done(),
                "report_every": REPORT_EVERY, "now": r.clock(),
                "tls_pin": request.app.state.tls_pin is not None,
                "update_command": install_command(request, str(request.base_url)),
                "unknown": sorted(named - r.companions.names())}

    @app.post("/api/companions/codes")
    async def add_host(body: NewHost, request: Request, _: Admin) -> dict:
        if body.hosts > 1 and body.name:
            raise HTTPException(422, "a command for several hosts names each after itself; "
                                     "leave the name empty")
        code, expires = rt(request).companions.new_code(body.name, body.read_only, body.hosts)
        return {"code": code, "expires": expires,
                "command": install_command(request, body.url, code, body.read_only)}

    def forget_host(request: Request, cid: str) -> str | None:
        name = rt(request).companions.remove(cid)
        if name is None:
            return None
        # Nothing of it stays to be inherited by a later host with the same name.
        request.app.state.patching.forget(name)
        request.app.state.adoption.forget(name)
        request.app.state.guests.forget(name)
        request.app.state.uninstalls.cancel(cid)
        return name

    @app.delete("/api/companions/{cid}")
    async def remove_host(cid: str, request: Request, _: Admin) -> dict:
        if forget_host(request, cid) is None:
            raise HTTPException(404, "no such host")
        return {"ok": True}

    @app.post("/api/companions/{cid}/uninstall")
    async def uninstall_host(cid: str, body: Confirm, request: Request, _: Admin) -> dict:
        """Uninstall the companion from the host, stacks handed back first."""
        if body.password != body.again:
            raise HTTPException(422, "the two passwords are not the same")
        auth_call(request.app.state.auth.confirm, body.password, address(request))
        row = next((v for v in rt(request).companions.views() if v["id"] == cid), None)
        if row is None:
            raise HTTPException(404, "no such host")
        if row["read_only"]:
            raise HTTPException(409, "a monitoring-only host is not Argus's to change: run "
                                     f"{UNINSTALL_COMMAND} on it, then Remove")
        return request.app.state.uninstalls.request(cid, row["name"])

    @app.post("/api/companions/uninstall")
    async def uninstall_all(body: Confirm, request: Request, _: Admin) -> dict:
        """Leave Argus: uninstall every host Argus may change, the password twice.
        Monitoring-only hosts are listed with the command for their owner."""
        if body.password != body.again:
            raise HTTPException(422, "the two passwords are not the same")
        auth_call(request.app.state.auth.confirm, body.password, address(request))
        un, asked = request.app.state.uninstalls, []
        for v in rt(request).companions.views():
            if not v["read_only"] and not un.to_send(v["id"]):
                un.request(v["id"], v["name"])
                asked.append(v["name"])
        return {"asked": asked}

    @app.get("/api/leave")
    async def leave_view(request: Request, _: Admin) -> dict:
        views = rt(request).companions.views()
        home = request.app.state.selfupdate.host_of(views)
        return {"hosts": [{"id": v["id"], "name": v["name"], "read_only": v["read_only"],
                           "uninstall": request.app.state.uninstalls.view(v["id"])}
                          for v in views],
                "uninstalled": request.app.state.uninstalls.done(),
                "outside": leave.outside(request.app.state.settings,
                                         request.app.state.alerter.watchers()),
                "argus_host": home[0]["name"] if home else None}

    @app.delete("/api/companions/{cid}/uninstall")
    async def cancel_uninstall(cid: str, request: Request, _: Admin) -> dict:
        request.app.state.uninstalls.cancel(cid)
        return {"ok": True}

    @app.put("/api/companions/{cid}/name")
    async def rename_host(cid: str, body: HostName, request: Request, _: Admin) -> dict:
        # Hosts name themselves after their hostname, and two Proxmox servers both
        # called pve, or a VM that calls itself localhost, need a better one.
        try:
            old = rt(request).companions.rename(cid, body.name)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        if old is None:
            raise HTTPException(404, "no such host")
        if old != body.name:
            request.app.state.patching.rename(old, body.name)
            request.app.state.adoption.rename(old, body.name)
            request.app.state.guests.rename(old, body.name)
        return {"ok": True, "name": body.name}

    @app.post("/api/companions/{cid}/guests/{vmid}/install")
    async def install_in_guest(cid: str, vmid: int, body: GuestInstall, request: Request,
                               _: Admin) -> dict:
        r = rt(request)
        server = next((c for c in r.companions.views() if c["id"] == cid), None)
        if server is None:
            raise HTTPException(404, "no such host")
        if server["read_only"]:
            raise HTTPException(409, f"{server['name']} is monitoring only")
        guests = ((server["report"] or {}).get("proxmox") or {}).get("guests") or []
        g = next((x for x in guests if x["vmid"] == vmid), None)
        if g is None:
            raise HTTPException(404, f"{server['name']} has no guest {vmid}")
        if g["status"] != "running":
            raise HTTPException(409, f"{g['name'] or vmid} is not running")
        name = name_from(g["name"]) if g["name"] else f"guest-{vmid}"
        if name in r.companions.names():
            raise HTTPException(409, f"a host called {name} is already in Argus")
        url = body.url.rstrip("/")
        pin = request.app.state.tls_pin if url.startswith("https://") else None
        # An hour, as for several hosts: the server adds guests one at a time.
        code, _expires = r.companions.new_code(name, ttl=GIVE_UP)
        request.app.state.guests.request(server["name"], vmid, g["type"], name, url, pin, code)
        return {"ok": True, "name": name}

    @app.post("/api/companions/{cid}/reinstall")
    async def reinstall_companion(cid: str, body: GuestInstall, request: Request,
                                  _: Admin) -> dict:
        """Reinstall a Proxmox guest's companion through its server, keeping the
        enrollment. Other hosts get the command to run (409, the UI shows it)."""
        r = rt(request)
        views = r.companions.views()
        host = next((c for c in views if c["id"] == cid), None)
        if host is None:
            raise HTTPException(404, "no such host")
        names = {host["name"], ((host["report"] or {}).get("hostname") or "").split(".")[0]}
        for server in views:
            if server["read_only"] or server["status"] != "ok" or server["id"] == cid:
                continue
            for g in ((server["report"] or {}).get("proxmox") or {}).get("guests") or []:
                if g["status"] == "running" and g["name"] and name_from(g["name"]) in names:
                    # A server companion from before reinstalls would enroll the guest
                    # again with the request id as a code, and fail: wait for it.
                    mine = os.environ.get("ARGUS_COMMIT", "")[:12]
                    theirs = (server["report"] or {}).get("version") or ""
                    if mine and mine != "unknown" and not theirs.endswith("+" + mine):
                        raise HTTPException(409, f"{server['name']} is updating its companion;"
                                                 " try again in a few minutes")
                    url = body.url.rstrip("/")
                    pin = request.app.state.tls_pin if url.startswith("https://") else None
                    raw = base64.b32encode(secrets.token_bytes(10)).decode().lower()
                    # A request id in a code's shape; never a code, nothing enrolls with it.
                    rid = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
                    request.app.state.guests.request(server["name"], g["vmid"], g["type"],
                                                     host["name"], url, pin, rid, reinstall=True)
                    return {"by": server["name"], "vmid": g["vmid"]}
        raise HTTPException(409, f"{host['name']} is not a running guest of a Proxmox server"
                                 " with Argus: run the command on it")

    @app.put("/api/companions/{cid}/ignored")
    async def ignore_container(cid: str, body: Ignore, request: Request, _: Admin) -> dict:
        names = rt(request).companions.ignore(cid, body.container, body.ignored)
        if names is None:
            raise HTTPException(404, "no such host")
        return {"ignored": names}

    # --- companions: the hosts' side ---

    @app.get("/companion/install.sh")
    async def install_sh() -> Response:
        return Response(INSTALL_SH.read_text(), media_type="text/x-shellscript")

    @app.get("/companion/download/{build}")
    async def download(build: Literal["linux-amd64", "linux-arm64"]) -> Response:
        path = COMPANION_DIR / f"argus-companion-{build}"
        if not path.is_file():
            raise HTTPException(404, "this Argus has no companion build for " + build)
        return FileResponse(path, media_type="application/octet-stream",
                            filename=f"argus-companion-{build}")

    @app.post("/companion/enroll")
    async def enroll(body: Enroll, request: Request) -> dict:
        try:
            cid, name, token = rt(request).companions.enroll(body.code, body.hostname,
                                                             address(request))
        except EnrollError as e:
            raise HTTPException(403, str(e)) from None
        return {"id": cid, "name": name, "token": token,
                "public_key": request.app.state.signer.public, "report_every": REPORT_EVERY}

    @app.post("/companion/report")
    async def companion_report(request: Request) -> Response:
        r = rt(request)
        auth = request.headers.get("authorization", "")
        row = r.companions.by_token(auth.removeprefix("Bearer ").strip())
        if row is None:
            raise HTTPException(401, "unknown companion token; enroll this host again")
        body = await request.body()
        if len(body) > MAX_COMPANION_REPORT:
            raise HTTPException(413, "report too large")
        try:
            rep = Report.model_validate_json(body)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        for c in rep.checks:
            r.record_remote(row["name"], c.name, Result(c.ok, c.duration, c.error))
        # Collected files (with a .env) stay in memory for the preview, never stored.
        r.companions.save_report(row["id"],
                                 rep.model_dump(exclude={"nonce", "checks", "collected",
                                                         "guest_installs", "argus_update",
                                                         "uninstall"}),
                                 address(request))
        if not row["read_only"]:
            request.app.state.adoption.received(row["name"],
                                                [c.model_dump() for c in rep.collected])
        pt = request.app.state.patching
        pt.record(row["name"], rep.patch and rep.patch.model_dump())
        gi = request.app.state.guests
        gi.results(row["name"], [x.model_dump() for x in rep.guest_installs])
        su = request.app.state.selfupdate
        su.result(row["name"], rep.argus_update and rep.argus_update.model_dump())
        un = request.app.state.uninstalls
        if un.result(row["id"], rep.uninstall and rep.uninstall.model_dump()) == "ok":
            # Its stacks are handed back and it is removing itself: forget it now.
            name = forget_host(request, row["id"])
            logging.getLogger("argus").info("companion on %s uninstalled", name)
            reply = json.dumps({"name": row["name"], "report_every": REPORT_EVERY,
                                "read_only": bool(row["read_only"]), "checks": []},
                               separators=(",", ":")).encode()
            return Response(reply, media_type="application/json", headers={
                "X-Argus-Signature": request.app.state.signer.sign(row["id"], rep.nonce, reply)})
        body = {
            "name": row["name"],
            "report_every": REPORT_EVERY,
            "read_only": bool(row["read_only"]),
            "checks": [c.model_dump(include={"name", "kind", "target", "interval", "timeout",
                                             "expect_status", "verify_tls"})
                       for c in r.companion_checks(row["name"])],
        }
        d = request.app.state.deploys
        if not row["read_only"] and d.snapshot is not None:
            # A stack already running from a folder Argus does not manage waits to be
            # linked on the host card; sending it would start a second copy.
            stacks = [s for s in d.stacks_for(row["name"], rep.model_dump())
                      if not s.get("held")]
            done = {(x.path, x.digest) for x in rep.deploys if x.result == "ok"}
            # A pull credential only when something is about to be deployed.
            due = any((s["path"], s["digest"]) not in done for s in stacks)
            body["deploy"] = {"commit": d.snapshot.commit, "stacks": stacks,
                              "pull": await d.pull_credential() if due else None}
        if not row["read_only"] and (want := request.app.state.adoption.to_collect(row["name"])):
            body["collect"] = want
        # A monitoring-only host keeps the program it has: it is not Argus's to change.
        if not row["read_only"] and (sha := companion_sha256(rep.arch)):
            body["companion"] = {"sha256": sha}
        if not row["read_only"] and un.to_send(row["id"]):
            body["uninstall"] = True
        if not row["read_only"] and (upd := su.to_send(row["name"])):
            body["argus_update"] = upd
        if not row["read_only"] and rep.proxmox and (todo := gi.to_send(row["name"])):
            body["guest_installs"] = todo
        if bot := request.app.state.alerter.bot_id():
            body["argus_bot"] = bot  # so "argus-companion watch" can refuse Argus's own bot
        if rep.watch is not None:
            # Even a monitoring-only host may watch: the bot is its owner's, not Argus's.
            upd = su.state()
            quiet = (upd["at"] + WATCH_QUIET
                     if upd and upd["state"] in ("waiting", "running") else None)
            body["watch"] = request.app.state.alerter.watch_reply(quiet)
        run = pt.grant(row["name"], bool(row["read_only"]), rep.model_dump())
        if run:
            body["patch"] = run
        reply = json.dumps(body, separators=(",", ":")).encode()
        sig = request.app.state.signer.sign(row["id"], rep.nonce, reply)
        return Response(reply, media_type="application/json",
                        headers={"X-Argus-Signature": sig})

    @app.get("/healthz")
    async def healthz(request: Request) -> Response:
        r = rt(request)
        ok = r.clock() - r.last_tick < STALL
        return Response("ok\n" if ok else "scheduler stalled\n", 200 if ok else 503,
                        media_type="text/plain")

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        return Response(generate_latest(request.app.state.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/info")
    async def info(request: Request) -> dict:
        r = rt(request)
        return {
            "version": os.environ.get("ARGUS_VERSION", "dev"),
            "commit": os.environ.get("ARGUS_COMMIT", "unknown"),
            "config": r.config_hash,
            "config_reload_ok": r.reload_ok,
            "last_tick": r.last_tick,
            "now": r.clock(),
            "groups": r.config.groups,
            "sites": r.config.sites,
            "links": [link.model_dump() for link in r.config.links],
            "jobs_intro": r.config.jobs_intro,
            "alerts": request.app.state.alerter.summary(),
        }

    def check_view(r: Runtime, c) -> dict:
        s = r.store.get(c.name)
        view = {**c.model_dump(), **{k: v for k, v in vars(s).items() if k != "name"}}
        pause = r.store.pause_of(c.name)
        # A paused check shows as paused everywhere; what its probe says stays
        # in probe_status.
        view["probe_status"], view["paused"] = s.status, pause and {
            k: pause[k] for k in ("reason", "since", "until")}
        if pause:
            view["status"] = "paused"
        return view

    @app.get("/api/checks")
    async def checks(request: Request) -> list[dict]:
        r = rt(request)
        return [check_view(r, c) for c in r.config.checks]

    def repo_views(request: Request) -> list[dict]:
        """Each polled repo's state and track, in the order they were polled."""
        r, gh = rt(request), request.app.state.gh
        if not gh.connected:
            return []
        checks = {c.name: c for c in r.config.checks}
        beats = {h["name"]: h for h in r.heartbeat_views()}
        stacks = r.config.github.stacks_repo if r.config.github else "the deploy repo"
        d, s = request.app.state.deploys, request.app.state.settings
        # (host, stack path) -> how that host's companion deployed it
        status = {(v["companion"], st["path"]): st["status"]
                  for v in d.views(r.companions.views()) for st in v["stacks"]}
        out = []
        for t in gh.targets:
            state = gh.repos.get(t.name)
            if state is None:
                continue
            placed = None
            if not t.repo.stack and not t.repo.deploy:
                placed = [{**p, "status": {h: status.get((h, p["path"]), "pending")
                                           for h in p["hosts"]}}
                          for p in d.placements(t.owner, t.repo.name)] or None
            # argus.yml's check:, else the one named after the repo.
            linked = t.repo.check or github.check_for(t.repo.name, r.config.checks)
            c = checks.get(linked)
            out.append({**state, "check": linked or None, "track": github.track(
                state, c and check_view(r, c), beats, r.clock(),
                s.get("deploy.repo") or stacks, t.repo.deploy, placed,
                can_bump=s.has_secret(WRITE_TOKEN))})
        return out

    @app.get("/api/checks/{name}")
    async def check(name: str, request: Request) -> dict:
        r = rt(request)
        for c in r.config.checks:
            if c.name == name:
                repo = next((x for x in repo_views(request) if x["check"] == name), None)
                return {**check_view(r, c), "transitions": r.store.transitions(name),
                        "repo": repo and repo["name"], "track": repo and repo["track"]}
        raise HTTPException(404, "no such check")

    @app.put("/api/checks/{name}/pause")
    async def pause_check(name: str, body: Pause, request: Request, _: Admin) -> dict:
        r = rt(request)
        if name not in {c.name for c in r.config.checks}:
            raise HTTPException(404, "no such check")
        r.store.pause(name, body.reason.strip(), body.until, r.clock())
        await request.app.state.alerter.evaluate()
        return await check(name, request)

    @app.delete("/api/checks/{name}/pause")
    async def resume_check(name: str, request: Request, _: Admin) -> dict:
        r = rt(request)
        if name not in {c.name for c in r.config.checks}:
            raise HTTPException(404, "no such check")
        r.store.resume(name, r.clock())
        await request.app.state.alerter.evaluate()
        return await check(name, request)

    @app.get("/api/heartbeats")
    async def heartbeats(request: Request) -> list[dict]:
        return rt(request).heartbeat_views()

    @app.get("/api/jobs")
    async def jobs(request: Request) -> list[dict]:
        return rt(request).job_views()

    @app.get("/api/jobs/{name}")
    async def job(name: str, request: Request) -> dict:
        r = rt(request)
        for view in r.job_views():
            if view["name"] == name:
                return {**view, "runs": r.store.runs(name)}
        raise HTTPException(404, "no such job")

    @app.get("/api/updates")
    async def updates(request: Request) -> dict:
        r, gh = rt(request), request.app.state.gh
        return {
            "enabled": gh.connected,
            "owners": sorted({t.owner for t in gh.targets}),
            "last_success": gh.last_success,
            "last_error": gh.shown_error or gh.found_error,
            "now": r.clock(),
            "repos": repo_views(request),
        }

    # The one write: a timer saying "I ran". Only configured names are accepted.
    # A timer that deploys an app adds ?version=<commit> for what runs now.
    @app.post("/heartbeat/{name}", status_code=204)
    async def heartbeat(name: str, request: Request, version: str | None = None) -> Response:
        source = request.client.host if request.client else "unknown"
        if version is not None and not re.fullmatch(r"[0-9a-f]{7,40}", version):
            raise HTTPException(422, "version must be a commit sha")
        if not rt(request).beat(name, source, version):
            raise HTTPException(404, "no such heartbeat")
        return Response(status_code=204)

    # The other write: a scheduled run reporting how it went (decision 0017).
    # Only configured names are accepted, and the body is size-capped.
    @app.post("/job/{name}", status_code=204)
    async def report(name: str, request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_REPORT:
            raise HTTPException(413, "report too large")
        try:
            run = JobRun.model_validate_json(body)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        source = request.client.host if request.client else "unknown"
        if not rt(request).report(name, run.model_dump(), source):
            raise HTTPException(404, "no such job")
        return Response(status_code=204)

    # The read-only UI (ui/, built into the image). Mounted last so the API
    # routes above win; absent in development until `npm run build`.
    if UI_DIR.is_dir():
        app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")

    # The page shell names the build's hashed assets, so a browser must ask for
    # it again each time or it keeps showing the previous UI after a deploy.
    @app.middleware("http")
    async def fresh_shell(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app
