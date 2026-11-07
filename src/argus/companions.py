"""Companions: one small program per host that reports what outside checks cannot
see (containers, disks, failed units, pending updates) and runs checks from
inside the host. They only ever call Argus; nothing listens on a host.

Enrollment (design: "Companions in detail"):

- "Add a host" makes a code: single use, valid for 15 minutes, stored hashed.
- The install command carries the code, Argus's URL and the pin of Argus's
  TLS key, so the companion's first call already refuses anyone else.
- The companion trades the code for its own token (256 random bits; Argus keeps
  only a hash) and Argus's ed25519 public key, which it pins.
- Every reply to a report is signed over the companion's id and a nonce the
  companion chose, so a proxy or a replay cannot tell a host what to run.
  (M2's replies carry only checks; deploys arrive in M4 under the same seal.)
- Removing a host deletes its token at once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from argus.settings import Settings

CODE_TTL = 15 * 60
SEVERAL_TTL = 60 * 60  # a code for several hosts: time to go round them
MAX_USES = 50
REPORT_EVERY = 60  # seconds, told to each companion in every reply
QUIET_AFTER = 3 * REPORT_EVERY  # three missed reports
DISK_FULL = 0.95  # used share that pages
DISK_WARN = 0.85  # used share the UI marks
SIGNING_KEY = "companions.signing_key"
# What a signature covers: this prefix, the companion's id, its nonce, the body.
DOMAIN = b"argus-companion-reply-v1\n"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_codes (
    code_hash TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL,
    name TEXT NOT NULL, read_only INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS companions (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE,
    enrolled REAL NOT NULL, last_seen REAL, address TEXT NOT NULL DEFAULT '',
    read_only INTEGER NOT NULL, report TEXT, ignored TEXT NOT NULL DEFAULT '[]'
);
"""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def name_from(hostname: str) -> str:
    """A host name as a companion name: lowercase, letters, digits and dashes."""
    short = hostname.split(".")[0].lower()
    name = re.sub(r"[^a-z0-9-]+", "-", short).strip("-")[:63]
    return name if NAME.match(name) else "host"


class EnrollError(Exception):
    pass


class Companions:
    def __init__(self, db: sqlite3.Connection, clock: Callable[[], float]) -> None:
        self.db = db
        self.clock = clock
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(companion_codes)")}
        if "uses" not in cols:  # installs from before codes for several hosts
            with self.db:
                self.db.execute("ALTER TABLE companion_codes"
                                " ADD COLUMN uses INTEGER NOT NULL DEFAULT 1")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(companions)")}
        if "ignored" not in cols:  # installs from before containers could be ignored
            with self.db:
                self.db.execute("ALTER TABLE companions"
                                " ADD COLUMN ignored TEXT NOT NULL DEFAULT '[]'")

    # --- enrollment ---

    def new_code(self, name: str = "", read_only: bool = False,
                 uses: int = 1, ttl: float | None = None) -> tuple[str, float]:
        """A code for `uses` hosts (one by default). `name` is optional and only for
        one host; empty means each host's own name. A code for several hosts lasts
        an hour, so one command can be run on each guest in turn."""
        if uses > 1 and name:
            raise ValueError("a code for several hosts names each host after itself")
        uses = max(1, min(uses, MAX_USES))
        now = self.clock()
        ttl = ttl or (SEVERAL_TTL if uses > 1 else CODE_TTL)
        raw = base64.b32encode(secrets.token_bytes(10)).decode().lower()
        code = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
        with self.db:
            self.db.execute("DELETE FROM companion_codes WHERE expires < ?", (now,))
            self.db.execute("INSERT INTO companion_codes (code_hash, created, expires, name,"
                            " read_only, uses) VALUES (?, ?, ?, ?, ?, ?)",
                            (_hash(code), now, now + ttl, name, int(read_only), uses))
        return code, now + ttl

    def enroll(self, code: str, hostname: str, address: str) -> tuple[str, str, str]:
        """Trade a code for a token. Returns (id, name, token)."""
        now = self.clock()
        with self.db:
            row = self.db.execute("SELECT * FROM companion_codes WHERE code_hash = ?",
                                  (_hash(code.strip().lower()),)).fetchone()
            if row is None or row["expires"] < now:
                raise EnrollError("this code is unknown, used or expired; make a new one in Argus")
            if row["uses"] > 1:
                self.db.execute("UPDATE companion_codes SET uses = uses - 1 WHERE code_hash = ?",
                                (row["code_hash"],))
            else:
                self.db.execute("DELETE FROM companion_codes WHERE code_hash = ?",
                                (row["code_hash"],))
            base = row["name"] or name_from(hostname)
            taken = {r[0] for r in self.db.execute("SELECT name FROM companions")}
            name, n = base, 2
            while name in taken:
                name, n = f"{base[:60]}-{n}", n + 1
            cid, token = secrets.token_hex(8), "argc_" + secrets.token_urlsafe(32)
            self.db.execute(
                "INSERT INTO companions (id, name, token_hash, enrolled, address, read_only)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (cid, name, _hash(token), now, address, row["read_only"]))
        return cid, name, token

    def by_token(self, token: str) -> sqlite3.Row | None:
        if not token.startswith("argc_"):
            return None
        return self.db.execute("SELECT * FROM companions WHERE token_hash = ?",
                               (_hash(token),)).fetchone()

    def remove(self, cid: str) -> str | None:
        """Remove a host; returns its name, or None when there was no such host."""
        with self.db:
            row = self.db.execute("SELECT name FROM companions WHERE id = ?", (cid,)).fetchone()
            self.db.execute("DELETE FROM companions WHERE id = ?", (cid,))
        return row and row["name"]

    def rename(self, cid: str, name: str) -> str | None:
        """Give a host a new name; returns the old one, or None when there was no such
        host. The companion never uses its name, so nothing changes on the host."""
        if not NAME.match(name):
            raise ValueError("a name is lowercase letters, digits and dashes")
        with self.db:
            row = self.db.execute("SELECT name FROM companions WHERE id = ?", (cid,)).fetchone()
            if row is None or row["name"] == name:
                return row and row["name"]
            if self.db.execute("SELECT 1 FROM companions WHERE name = ?", (name,)).fetchone():
                raise ValueError(f"another host is already called {name}")
            self.db.execute("UPDATE companions SET name = ? WHERE id = ?", (name, cid))
        return row["name"]

    # --- reports ---

    def save_report(self, cid: str, report: dict, address: str) -> None:
        # A companion reads Proxmox in the background, so its first reports after a
        # restart (every self-update) have no guests yet. Keep the last reading
        # until a new one arrives, or the server's guests would jump around.
        if report.get("proxmox") is None and str(report.get("kernel", "")).endswith("-pve"):
            row = self.db.execute("SELECT report FROM companions WHERE id = ?", (cid,)).fetchone()
            before = json.loads(row["report"]) if row and row["report"] else {}
            if before.get("proxmox"):
                report = {**report, "proxmox": before["proxmox"]}
        with self.db:
            self.db.execute("UPDATE companions SET last_seen = ?, address = ?, report = ?"
                            " WHERE id = ?", (self.clock(), address, json.dumps(report), cid))

    def views(self) -> list[dict]:
        """Every companion with its newest report and a status: waiting (enrolled,
        no report yet), ok or quiet."""
        now, out = self.clock(), []
        for r in self.db.execute("SELECT * FROM companions ORDER BY name"):
            seen = r["last_seen"]
            status = "waiting" if seen is None else (
                "quiet" if now - seen > QUIET_AFTER else "ok")
            out.append({"id": r["id"], "name": r["name"], "enrolled": r["enrolled"],
                        "last_seen": seen, "age": seen and now - seen, "status": status,
                        "address": r["address"], "read_only": bool(r["read_only"]),
                        "report": json.loads(r["report"]) if r["report"] else None,
                        "ignored": json.loads(r["ignored"])})
        return out

    def ignore(self, cid: str, container: str, ignored: bool) -> list[str] | None:
        """Containers the owner does not care about (a one-off that exited, say): the
        page shows them apart and does not count them as trouble. Returns the new
        list, or None when there was no such host."""
        with self.db:
            row = self.db.execute("SELECT ignored FROM companions WHERE id = ?",
                                  (cid,)).fetchone()
            if row is None:
                return None
            names = set(json.loads(row["ignored"]))
            names = sorted(names | {container} if ignored else names - {container})
            self.db.execute("UPDATE companions SET ignored = ? WHERE id = ?",
                            (json.dumps(names), cid))
        return names

    def report_of(self, name: str) -> dict | None:
        row = self.db.execute("SELECT report FROM companions WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def ignored_of(self, name: str) -> list[str]:
        row = self.db.execute("SELECT ignored FROM companions WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row else []

    def names(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT name FROM companions")}


class Signer:
    """Argus's ed25519 key, made once and kept sealed in the settings store."""

    def __init__(self, settings: Settings) -> None:
        raw = settings.secret(SIGNING_KEY)
        if raw is None:
            key = Ed25519PrivateKey.generate()
            raw = base64.b64encode(key.private_bytes(
                Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
            settings.set_secret(SIGNING_KEY, raw)
        self._key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))
        self.public = base64.b64encode(
            self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    def sign(self, cid: str, nonce: str, body: bytes) -> str:
        msg = DOMAIN + cid.encode() + b"\n" + nonce.encode() + b"\n" + body
        return base64.b64encode(self._key.sign(msg)).decode()
