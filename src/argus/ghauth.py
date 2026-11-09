"""Connecting Argus to GitHub, and the credentials the poller uses.

Three sources, best first:

- **A GitHub App Argus made itself** ("Connect GitHub"). GitHub's app-manifest
  flow: Argus sends the browser to GitHub with a manifest (name, read-only
  permissions); the user confirms, GitHub sends the browser back with a
  one-time code, and Argus trades it for the app's id and private key. The
  user then installs the app on the repos they choose. Argus signs a short
  JWT with the key to get an installation token per account, valid one hour.
- **A fine-grained token** pasted on the GitHub page, for people who cannot
  make apps.
- **ARGUS_GITHUB_TOKEN_FILE**, as v1 does (this homelab).

The private key and the token are sealed in the settings store and never shown.
No webhooks: Argus polls, so it works behind NAT with nothing exposed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from argus.settings import Settings

log = logging.getLogger("argus.github")

API = "https://api.github.com"
WEB = "https://github.com"
APP = "github.app"  # {id, slug, html_url, owner, owner_type, name}
APP_KEY = "github.app_key"  # secret: the app's private key, PEM
TOKEN = "github.token"  # noqa: S105 (a setting name) secret: a pasted fine-grained token
STATE = "github.manifest_state"  # {hash, expires}: the pending "Connect GitHub"
HIDDEN = "github.hidden"  # full names the user chose not to show
STATE_TTL = 3600
REDISCOVER = 600  # seconds between looking for new repos
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": "argus"}

# Read only, and only what the Updates view reads. M4 asks for contents:write on
# the deploy repo when deploys arrive; GitHub asks the user to approve that.
PERMISSIONS = {"contents": "read", "pull_requests": "read", "actions": "read",
               "vulnerability_alerts": "read", "metadata": "read", "packages": "read"}


# Opening the pipeline pull request (decision 20): asked of GitHub per click,
# for that one repo, and never kept.
PIPELINE_WRITE = {"contents": "write", "pull_requests": "write", "workflows": "write"}


class NeedsPermission(Exception):
    """The GitHub App has not been given the write permissions yet."""


class Credentials(Protocol):
    async def token_for(self, owner: str | None) -> str | None: ...
    async def repos(self) -> list[dict]: ...
    def describe(self) -> dict: ...


def _repo(r: dict) -> dict:
    return {"full_name": r["full_name"], "owner": r["owner"]["login"], "name": r["name"],
            "default_branch": r.get("default_branch") or "main", "private": r.get("private"),
            "archived": bool(r.get("archived") or r.get("disabled")), "url": r["html_url"]}


async def _pages(client: httpx.AsyncClient, url: str, token: str, key: str | None = None,
                 limit: int = 10, **params) -> list[dict]:
    """Every item of a paged list (100 a page, `limit` pages at most)."""
    out: list[dict] = []
    for page in range(1, limit + 1):
        r = await client.get(f"{API}{url}", params={"per_page": 100, "page": page, **params},
                             headers={**HEADERS, "Authorization": f"Bearer {token}"})
        r.raise_for_status()
        items = r.json()[key] if key else r.json()
        out += items
        if len(items) < 100:
            break
    return out


class StaticToken:
    """One token for every repo: pasted on the GitHub page, or v1's token file."""

    def __init__(self, token: str, client: httpx.AsyncClient, source: str = "token") -> None:
        self._token, self.client, self.source = token, client, source

    async def token_for(self, owner: str | None) -> str | None:
        return self._token

    async def write_token(self, owner: str, repo: str) -> str:
        return self._token  # whether it may write is GitHub's answer, per request

    async def repos(self) -> list[dict]:
        # Yours and your organizations': not every repo you were once invited to.
        found = await _pages(self.client, "/user/repos", self._token,
                             affiliation="owner,organization_member")
        return [_repo(r) for r in found]

    def describe(self) -> dict:
        return {"source": self.source}


class AppCredentials:
    """Argus's own GitHub App: installation tokens per account, renewed before
    they expire."""

    def __init__(self, app: dict, pem: str, client: httpx.AsyncClient,
                 clock: Callable[[], float] = time.time) -> None:
        self.app, self.client, self.clock = app, client, clock
        self._key = serialization.load_pem_private_key(pem.encode(), password=None)
        self._tokens: dict[int, tuple[str, float]] = {}  # installation id -> (token, expires)
        self.installations: list[dict] = []  # {id, account, url, selection}
        self._listed = 0.0

    def jwt(self) -> str:
        now = int(self.clock())
        enc = lambda d: base64.urlsafe_b64encode(  # noqa: E731
            json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=")
        head = enc({"alg": "RS256", "typ": "JWT"}) + b"." + enc(
            {"iat": now - 60, "exp": now + 540, "iss": str(self.app["id"])})
        sig = self._key.sign(head, padding.PKCS1v15(), hashes.SHA256())
        return (head + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()

    async def _list(self, force: bool = False) -> list[dict]:
        if force or not self.installations or self.clock() - self._listed > REDISCOVER:
            r = await self.client.get(f"{API}/app/installations",
                                      headers={**HEADERS, "Authorization": f"Bearer {self.jwt()}"})
            r.raise_for_status()
            self.installations = [{"id": i["id"], "account": i["account"]["login"],
                                   "url": i["html_url"], "selection": i["repository_selection"]}
                                  for i in r.json()]
            self._listed = self.clock()
        return self.installations

    async def _token(self, iid: int) -> str:
        tok = self._tokens.get(iid)
        if tok and tok[1] - self.clock() > 300:
            return tok[0]
        r = await self.client.post(f"{API}/app/installations/{iid}/access_tokens",
                                   headers={**HEADERS, "Authorization": f"Bearer {self.jwt()}"})
        r.raise_for_status()
        # GitHub's tokens last an hour; renew five minutes early, by our clock.
        self._tokens[iid] = (r.json()["token"], self.clock() + 3600)
        return self._tokens[iid][0]

    async def pull_token(self, owner: str) -> str | None:
        """A token that can only read packages, for one deploy's image pulls.
        Kept for its hour like the others, so a busy minute makes one, not many."""
        installs = await self._list()
        match = [i for i in installs if i["account"].lower() == owner.lower()]
        if not match:
            return None
        key = -match[0]["id"]  # beside the full token for the same installation
        tok = self._tokens.get(key)
        if tok and tok[1] - self.clock() > 300:
            return tok[0]
        r = await self.client.post(
            f"{API}/app/installations/{match[0]['id']}/access_tokens",
            headers={**HEADERS, "Authorization": f"Bearer {self.jwt()}"},
            json={"permissions": {"packages": "read"}})
        r.raise_for_status()
        self._tokens[key] = (r.json()["token"], self.clock() + 3600)
        return self._tokens[key][0]

    async def write_token(self, owner: str, repo: str) -> str:
        """A token that can write to one repo only, for one pull request. GitHub
        refuses it until the owner has given the app those permissions."""
        installs = await self._list(force=True)
        match = [i for i in installs if i["account"].lower() == owner.lower()]
        if not match:
            raise NeedsPermission(f"the GitHub App is not installed on {owner}")
        r = await self.client.post(
            f"{API}/app/installations/{match[0]['id']}/access_tokens",
            headers={**HEADERS, "Authorization": f"Bearer {self.jwt()}"},
            json={"repositories": [repo], "permissions": PIPELINE_WRITE})
        if r.status_code == 422:
            raise NeedsPermission("the GitHub App may not write to repos yet")
        r.raise_for_status()
        return r.json()["token"]

    async def token_for(self, owner: str | None) -> str | None:
        installs = await self._list()
        match = [i for i in installs if owner and i["account"].lower() == owner.lower()]
        pick = match[0] if match else (installs[0] if installs else None)
        return await self._token(pick["id"]) if pick else None

    async def repos(self) -> list[dict]:
        out = []
        for i in await self._list(force=True):
            found = await _pages(self.client, "/installation/repositories",
                                 await self._token(i["id"]), key="repositories")
            out += [_repo(r) for r in found]
        return out

    def describe(self) -> dict:
        return {"source": "app", "app": self.app, "installations": self.installations,
                "install_url": f"{self.app['html_url']}/installations/new"}


def credentials(settings: Settings, client: httpx.AsyncClient, token_file: Path,
                clock: Callable[[], float] = time.time) -> Credentials | None:
    """The best source configured right now."""
    app, pem = settings.get(APP), settings.secret(APP_KEY)
    if app and pem:
        return AppCredentials(app, pem, client, clock)
    token = settings.secret(TOKEN)
    if token:
        return StaticToken(token, client)
    try:
        text = token_file.read_text().strip()
    except OSError:
        return None
    return StaticToken(text, client, "file") if text else None


# --- "Connect GitHub" ---

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def manifest(settings: Settings, base_url: str, name: str, org: str,
             clock: Callable[[], float] = time.time) -> dict:
    """Where the browser posts the manifest, and the manifest itself."""
    state = secrets.token_urlsafe(32)
    settings.set(STATE, {"hash": _hash(state), "expires": clock() + STATE_TTL})
    base = base_url.rstrip("/")
    where = f"{WEB}/organizations/{org}/settings/apps/new" if org else f"{WEB}/settings/apps/new"
    body = {
        "name": name, "url": base, "public": False,
        "description": "Argus reads this account's update PRs, CI runs and security alerts.",
        "redirect_url": f"{base}/github/manifest-callback",
        # Back to Argus after the user installs it on their repos.
        "setup_url": f"{base}/#/github", "setup_on_update": True,
        "default_permissions": PERMISSIONS, "default_events": [],
    }
    return {"action": f"{where}?state={state}", "manifest": json.dumps(body)}


class ConnectError(Exception):
    pass


async def finish(settings: Settings, client: httpx.AsyncClient, code: str, state: str,
                 clock: Callable[[], float] = time.time) -> dict:
    """GitHub sent the browser back: check the state, trade the code for the app."""
    pending = settings.get(STATE)
    settings.set(STATE, None)  # single use, whatever happens
    if not pending or pending["expires"] < clock() or not secrets.compare_digest(
            pending["hash"], _hash(state)):
        raise ConnectError("this link is old or was not started here; press Connect GitHub again")
    r = await client.post(f"{API}/app-manifests/{code}/conversions", headers=HEADERS)
    if r.status_code != 201:
        raise ConnectError(f"GitHub did not hand over the app (HTTP {r.status_code})")
    d = r.json()
    app = {"id": d["id"], "slug": d["slug"], "name": d["name"], "html_url": d["html_url"],
           "owner": d["owner"]["login"], "owner_type": d["owner"]["type"]}
    settings.set_secret(APP_KEY, d["pem"])
    settings.set(APP, app)
    settings.set_secret(TOKEN, None)  # the app replaces a pasted token
    log.info("GitHub App %s made on %s", app["slug"], app["owner"])
    return app


def settings_url(app: dict) -> str:
    """Where the user deletes or changes the app on GitHub."""
    if app.get("owner_type") == "Organization":
        return f"{WEB}/organizations/{app['owner']}/settings/apps/{app['slug']}"
    return f"{WEB}/settings/apps/{app['slug']}"
