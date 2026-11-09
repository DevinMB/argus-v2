"""Who sends alerts, and Argus's own sender.

The Alerts page picks the sender:

- `argus` (the default): this module pages through Telegram when a critical
  check is down, a critical job fails or is overdue, a critical heartbeat or
  any companion goes quiet, or a disk a companion reports is almost full;
  it repeats while the problem lasts and says when it clears.
- `prometheus`: Argus stays quiet and /metrics plus the rules from
  prometheus_rules() do it, as in v1.

Switching never leaves a gap. Argus keeps sending until the user confirms that
Alertmanager's test alert arrived; going back, Argus takes over only after its
own test arrived (switch()).

Several problems that start together go out as one message, so an outage of
the network is one page, not twenty.

Secrets: the bot token is part of Telegram's URL, so errors are rebuilt from
the response and never carry the URL, and httpx's request log is off.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from argus.companions import DISK_FULL, QUIET_AFTER
from argus.runtime import Runtime
from argus.settings import Settings

log = logging.getLogger("argus")
# httpx logs each request's URL at INFO, and Telegram's URL holds the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

EVALUATE_EVERY = 10  # seconds
RETRY_AFTER = 60  # seconds after a failed send
WATCH_EVERY = 60  # seconds between dead-man pings
SWITCH_WINDOW = 3600  # a test must be this recent to switch senders
PROM_TEST_FOR = 15 * 60  # how long argus_alert_test stays 1
DEFAULT_RULES = {"checks": True, "jobs": True, "heartbeats": True, "companions": True}
DEFAULT_REPEAT = 240  # minutes; 0 means never repeat

TOKEN = "alerts.telegram.token"  # noqa: S105 (a setting name, not a secret)
WATCH_URL = "watch.url"  # secret: a dead-man's switch URL holds its own key

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT NOT NULL, since REAL NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0, last_sent REAL
);
"""

Sender = Literal["argus", "prometheus"]


class SendError(Exception):
    """Why a message did not go out, safe to show and log (no URL, no token)."""


class Channel(Protocol):
    async def send(self, text: str) -> None: ...


class Telegram:
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self.chat_id = chat_id
        self.client = client

    async def send(self, text: str) -> None:
        try:
            r = await self.client.post(
                f"{self.API}/bot{self._token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4000],
                      "disable_web_page_preview": True},
                timeout=10,
            )
        except httpx.HTTPError as e:
            raise SendError(f"could not reach Telegram ({type(e).__name__})") from None
        if r.status_code != 200:
            try:
                why = str(r.json().get("description", ""))[:200]
            except ValueError:
                why = ""
            hint = {401: " (check the bot token)", 400: " (check the chat ID)",
                    403: " (the bot cannot post in that chat; start it or add it first)",
                    404: " (check the bot token)"}.get(r.status_code, "")
            raise SendError(f"Telegram refused the message: HTTP {r.status_code}"
                            f"{': ' + why if why else ''}{hint}")


async def recent_chats(token: str, client: httpx.AsyncClient) -> list[dict]:
    """Chats that messaged the bot lately, so the user can pick theirs instead of
    looking up a chat ID by hand. Telegram keeps updates for 24 hours."""
    try:
        r = await client.get(f"{Telegram.API}/bot{token}/getUpdates", timeout=10)
    except httpx.HTTPError as e:
        raise SendError(f"could not reach Telegram ({type(e).__name__})") from None
    if r.status_code != 200:
        hint = {401: "check the bot token", 404: "check the bot token",
                409: "the bot has a webhook; enter the chat ID by hand"}.get(r.status_code, "")
        raise SendError(f"Telegram said HTTP {r.status_code}{': ' + hint if hint else ''}")
    chats: dict[int, dict] = {}
    for u in r.json().get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if isinstance(chat.get("id"), int):
            name = (chat.get("title") or " ".join(
                filter(None, (chat.get("first_name"), chat.get("last_name"))))
                or chat.get("username") or str(chat["id"]))
            chats[chat["id"]] = {"id": str(chat["id"]), "name": name[:64],
                                 "kind": chat.get("type", "")}
    return list(chats.values())


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


@dataclass(frozen=True)
class Problem:
    title: str  # "web is down"
    detail: str  # what failed, one line
    path: str  # where in the UI, e.g. "#/check/web"


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    if s < 48 * 3600:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


class Alerter:
    def __init__(self, runtime: Runtime, settings: Settings, client: httpx.AsyncClient,
                 clock: Callable[[], float] | None = None) -> None:
        self.rt = runtime
        self.settings = settings
        self.client = client
        self.clock = clock or runtime.clock
        self.db: sqlite3.Connection = runtime.store.db
        self.db.executescript(SCHEMA)
        self._retry_at = 0.0
        self.deploys = None  # set by the app: failed deploys page under "companions"
        self.patching = None  # and so do failed or skipped patch runs

    # --- settings, as the UI sees them ---

    def sender(self) -> Sender:
        return self.settings.get("alerts.sender", "argus")

    def channel(self) -> Channel | None:
        token, chat = self.settings.secret(TOKEN), self.settings.get("alerts.telegram.chat_id")
        return Telegram(token, chat, self.client) if token and chat else None

    def view(self) -> dict:
        """Everything the Alerts page shows. Secrets only as set or not."""
        now = self.clock()
        return {
            "sender": self.sender(),
            "telegram": {"token_set": self.settings.has_secret(TOKEN),
                         "chat_id": self.settings.get("alerts.telegram.chat_id", "")},
            "rules": {**DEFAULT_RULES, **self.settings.get("alerts.rules", {})},
            "repeat_minutes": self.settings.get("alerts.repeat_minutes", DEFAULT_REPEAT),
            "link": self.settings.get("alerts.link", ""),
            "delivery": self.settings.get("alerts.delivery", {}),
            "test_ok_at": self.settings.get("alerts.test_ok_at"),
            "prom_test_at": self.settings.get("alerts.prom_test_at"),
            "prom_test_active": now < self.settings.get("alerts.prom_test_until", 0),
            "watch": {"url_set": self.settings.has_secret(WATCH_URL),
                      **self.settings.get("watch.state", {})},
            "watchers": self.watchers(),
            "watch_test": self.settings.get("watch.test", 0),
            "watch_elsewhere": self.settings.get("watch.elsewhere", ""),
            "watched_by": self.watched_by(),
            "firing": [dict(r) for r in self.db.execute(
                "SELECT key, title, detail, since, notified, last_sent FROM alerts"
                " ORDER BY since")],
            "now": now,
        }

    def summary(self) -> dict:
        """The public part, for /api/info: who sends and whether anything watches Argus."""
        return {"sender": self.sender(),
                "ready": self.sender() == "prometheus" or self.channel() is not None,
                "tested": self.settings.get("alerts.test_ok_at") is not None,
                "watched": self.watched_by()}

    # --- who watches Argus (decision 19) ---

    def bot_id(self) -> str:
        """The numeric ID of the bot Argus alerts with: public, unlike the token."""
        return (self.settings.secret(TOKEN) or "").split(":")[0]

    def watchers(self) -> list[dict]:
        """Hosts that watch Argus with their own bot (sudo argus-companion watch)."""
        own = self.bot_id()
        out = []
        for v in self.rt.companions.views():
            w = (v.get("report") or {}).get("watch")
            if not w:
                continue
            out.append({"name": v["name"], "status": v["status"], "bot": w.get("bot", ""),
                        "same_bot": bool(own) and w.get("bot_id") == own,
                        **{k: w.get(k) for k in ("checked_at", "error", "test", "test_error")}})
        return out

    def watched_by(self) -> str | None:
        """What watches Argus: hosts, a ping URL, something the owner named,
        Prometheus (its ArgusDown rule), or nothing."""
        if any(w["status"] == "ok" and not w["same_bot"] for w in self.watchers()):
            return "hosts"
        if self.settings.has_secret(WATCH_URL):
            return "url"
        if self.settings.get("watch.elsewhere"):
            return "elsewhere"
        if self.sender() == "prometheus":
            return "prometheus"  # the rules Argus hands it include ArgusDown
        return None

    def watch_reply(self, quiet_until: float | None) -> dict:
        """The watch part of a watcher's signed reply."""
        out: dict = {"test": self.settings.get("watch.test", 0)}
        if quiet_until:
            out["quiet_until"] = quiet_until
        d = self.settings.get("alerts.delivery") or {}
        if self.sender() == "argus" and d.get("error") and d.get("error_at", 0) > d.get("ok_at", 0):
            out["alerts_failing"] = d["error"]
        return out

    # --- what is wrong right now ---

    def problems(self) -> dict[str, Problem]:
        rules = {**DEFAULT_RULES, **self.settings.get("alerts.rules", {})}
        rt, now = self.rt, self.clock()
        many = len(rt.config.sites) > 1
        where = (lambda site: f" ({rt.config.sites.get(site, site)})") if many else (lambda _: "")
        out: dict[str, Problem] = {}
        if rules["checks"]:
            states, paused = rt.store.all(), rt.store.pauses()
            for c in rt.config.checks:
                s = states.get(c.name)
                if c.severity == "critical" and s and s.status == "down" and c.name not in paused:
                    out[f"check:{c.name}"] = Problem(
                        f"{c.name}{where(c.site)} is down",
                        f"{c.kind} {c.target}: {s.last_error or 'failed'}",
                        f"#/check/{c.name}")
        if rules["jobs"]:
            for j in rt.job_views():
                if j["severity"] != "critical":
                    continue
                name = j["title"] or j["name"]
                if j["status"] == "failed":
                    out[f"job:{j['name']}"] = Problem(
                        f"{name}{where(j['site'])} failed",
                        j["last"]["summary"] or "Its newest run reported a failure.", "#")
                elif j["status"] == "late":
                    out[f"job:{j['name']}"] = Problem(
                        f"{name}{where(j['site'])} has not run on schedule",
                        f"No report for {_ago(now - j['last']['finished'])}"
                        f" (limit {_ago(j['max_age'])}).", "#")
        if rules["heartbeats"]:
            for h in rt.heartbeat_views():
                if h["severity"] != "critical":
                    continue
                # Never seen: only once Argus has run longer than the limit, so a
                # restart or a new entry is not an instant page.
                quiet = h["status"] == "late" or (
                    h["status"] == "never" and now - rt.started > h["max_age"])
                if quiet:
                    seen = (f"Last beat {_ago(h['age'])} ago" if h["age"] is not None
                            else "No beat yet")
                    out[f"heartbeat:{h['name']}"] = Problem(
                        f"{h['name']}{where(h['site'])} has gone quiet",
                        f"{seen} (limit {_ago(h['max_age'])}).", "#")
        if rules["companions"] and self.patching is not None:
            for key, title, detail in self.patching.problems(
                    [c["name"] for c in rt.companions.views()]):
                out[key] = Problem(title, detail, "#/hosts")
        if rules["companions"]:
            for c in rt.companions.views():
                if c["status"] == "quiet":
                    out[f"companion:{c['name']}"] = Problem(
                        f"host {c['name']} has gone quiet",
                        f"No report from its companion for {_ago(c['age'])}"
                        f" (limit {_ago(QUIET_AFTER)}).", "#/hosts")
                    continue
                if self.deploys is not None:
                    for v in self.deploys.views([c]):
                        for st in v["stacks"]:
                            if st["status"] in ("failed", "refused", "rolled_back"):
                                word = {"refused": "was refused by its policy",
                                        "rolled_back": "failed and was rolled back"}.get(
                                            st["status"], "failed")
                                out[f"deploy:{c['name']}:{st['path']}"] = Problem(
                                    f"deploy of {st['name']} on {c['name']} {word}",
                                    st["detail"][-300:] or "See the Hosts page.", "#/hosts")
                for d in (c["report"] or {}).get("disks", []):
                    if d["total"] and d["used"] / d["total"] >= DISK_FULL:
                        out[f"disk:{c['name']}:{d['mount']}"] = Problem(
                            f"{d['mount']} on {c['name']} is almost full",
                            f"{d['used'] / d['total']:.0%} used,"
                            f" {_size(d['total'] - d['used'])} free.", "#/hosts")
        return out

    # --- sending ---

    def _link(self, path: str = "") -> str:
        base = self.settings.get("alerts.link", "").rstrip("/")
        return f"\n{base}/{path}" if base else ""

    async def _send(self, channel: Channel, text: str) -> bool:
        now = self.clock()
        prev = self.settings.get("alerts.delivery", {})
        try:
            await channel.send(text)
        except SendError as e:
            log.error("alert not sent: %s", e)
            self.settings.set("alerts.delivery", {**prev, "error": str(e), "error_at": now})
            self._retry_at = now + RETRY_AFTER
            return False
        self.settings.set("alerts.delivery", {"ok_at": now})
        return True

    async def evaluate(self) -> None:
        """Bring the alerts table in line with what is wrong, and send what is due."""
        now = self.clock()
        problems = self.problems()
        rows = {r["key"]: dict(r) for r in self.db.execute("SELECT * FROM alerts")}
        channel = self.channel() if self.sender() == "argus" else None

        with self.db:
            for key, p in problems.items():
                if key in rows:
                    self.db.execute("UPDATE alerts SET title = ?, detail = ? WHERE key = ?",
                                    (p.title, p.detail, key))
                else:
                    self.db.execute("INSERT INTO alerts (key, title, detail, since)"
                                    " VALUES (?, ?, ?, ?)", (key, p.title, p.detail, now))
            cleared = [rows[k] for k in rows if k not in problems]
            for r in cleared:
                self.db.execute("DELETE FROM alerts WHERE key = ?", (r["key"],))

        if channel is None:
            return
        # Pausing a check is not its recovery: no "resolved" for it.
        paused = {f"check:{n}" for n in self.rt.store.pauses()}
        told = [r for r in cleared if r["notified"] and r["key"] not in paused]
        if told:
            # One try: a missed "resolved" is not worth holding the table for.
            if len(told) == 1:
                r = told[0]
                text = f"✅ Resolved: {r['title']} (for {_ago(now - r['since'])})"
            else:
                text = f"✅ {len(told)} resolved\n" + "\n".join(
                    f"• {r['title']} (for {_ago(now - r['since'])})" for r in told)
            await self._send(channel, text + self._link())
        if now < self._retry_at:
            return

        rows = {r["key"]: dict(r) for r in self.db.execute("SELECT * FROM alerts ORDER BY since")}
        repeat = self.settings.get("alerts.repeat_minutes", DEFAULT_REPEAT) * 60
        new = [r for r in rows.values() if not r["notified"]]
        again = [r for r in rows.values() if r["notified"] and repeat
                 and now - (r["last_sent"] or 0) >= repeat]
        for batch, first in ((new, True), (again, False)):
            if not batch:
                continue
            if len(batch) == 1:
                r = batch[0]
                head = "🔴 " if first else f"🔁 Still, for {_ago(now - r['since'])}: "
                text = f"{head}{r['title']}\n{r['detail']}" + self._link(
                    problems[r["key"]].path if r["key"] in problems else "")
            else:
                head = (f"🔴 {len(batch)} problems" if first
                        else f"🔁 Still {len(batch)} problems")
                text = head + "\n" + "\n".join(
                    f"• {r['title']}: {r['detail']}" for r in batch) + self._link()
            if not await self._send(channel, text):
                return
            with self.db:
                self.db.executemany("UPDATE alerts SET notified = 1, last_sent = ? WHERE key = ?",
                                    [(now, r["key"]) for r in batch])

    async def test(self) -> str | None:
        """Send a test through the saved channel. Returns the error, or None."""
        channel = self.channel()
        if channel is None:
            return "Add a bot token and a chat ID first."
        now = self.clock()
        if not await self._send(channel, "✅ Test from Argus: alerts reach you." + self._link()):
            return self.settings.get("alerts.delivery", {}).get("error", "not sent")
        self.settings.set("alerts.test_ok_at", now)
        self._retry_at = 0.0
        return None

    def prometheus_test(self) -> None:
        """Make argus_alert_test 1 for a while, so ArgusTestAlert fires in Prometheus."""
        now = self.clock()
        self.settings.set("alerts.prom_test_at", now)
        self.settings.set("alerts.prom_test_until", now + PROM_TEST_FOR)

    def prometheus_test_active(self) -> bool:
        return self.clock() < self.settings.get("alerts.prom_test_until", 0)

    def switch(self, to: Sender) -> str | None:
        """Hand alerting to the other sender. Returns why not, or None."""
        now = self.clock()
        if to == self.sender():
            return None
        if to == "prometheus":
            if now - (self.settings.get("alerts.prom_test_at") or 0) > SWITCH_WINDOW:
                return "Send a test through Prometheus first, and wait for it to arrive."
        else:
            if self.channel() is None:
                return "Set up Telegram first."
            if now - (self.settings.get("alerts.test_ok_at") or 0) > SWITCH_WINDOW:
                return "Send a test from Argus first, and check that it arrived."
        self.settings.set("alerts.sender", to)
        if to == "argus":
            # Problems already open were Prometheus's to send; Argus sends them now
            # once, so a switch in the middle of an outage still tells you.
            with self.db:
                self.db.execute("UPDATE alerts SET notified = 0")
        log.info("alerts are now sent by %s", to)
        return None

    # --- who watches Argus ---

    async def ping_watcher(self) -> None:
        """Ping the dead-man's switch, but only while the scheduler is alive: a hung
        Argus must go quiet so the outside service notices."""
        url = self.settings.secret(WATCH_URL)
        if not url:
            return
        now = self.clock()
        if now - self.rt.last_tick > 30:
            return
        prev = self.settings.get("watch.state", {})
        try:
            r = await self.client.get(url, timeout=10)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            self.settings.set("watch.state", {**prev, "error": f"HTTP {e.response.status_code}",
                                              "error_at": now})
            return
        except httpx.HTTPError as e:
            self.settings.set("watch.state", {**prev, "error": f"unreachable ({type(e).__name__})",
                                              "error_at": now})
            return
        self.settings.set("watch.state", {"ok_at": now})

    async def run_forever(self) -> None:
        last_watch = 0.0
        while True:
            try:
                await self.evaluate()
                if self.clock() - last_watch >= WATCH_EVERY:
                    last_watch = self.clock()
                    await self.ping_watcher()
            except Exception:
                log.exception("alerting failed")
            await asyncio.sleep(EVALUATE_EVERY)


def prometheus_rules(link: str = "") -> str:
    """Rules for people who let Prometheus and Alertmanager page. Severity comes from
    each entry's own label; route severity="critical" to your phone."""
    base = link.rstrip("/") or "http://ARGUS"
    return f"""# Argus alert rules for Prometheus. Load them with rule_files and scrape
# Argus's /metrics (job name "argus"). Generated by Argus; the Alerts page has a
# fresh copy.
groups:
  - name: argus
    rules:
      - alert: ArgusDown
        expr: up{{job="argus"}} == 0 or absent(up{{job="argus"}})
        for: 5m
        labels: {{ severity: critical }}
        annotations:
          summary: "Argus is not answering"
          description: "Prometheus cannot scrape Argus, so nothing it watches is watched."

      - alert: ArgusStalled
        expr: time() - argus_scheduler_last_tick_timestamp_seconds > 120
        for: 2m
        labels: {{ severity: critical }}
        annotations:
          summary: "Argus's scheduler has stopped"
          description: "Argus answers but runs no checks. Restart its container."

      - alert: ArgusCheckDown
        expr: argus_check_up == 0 unless on (check) argus_check_paused == 1
        for: 1m
        annotations:
          summary: "{{{{ $labels.check }}}} is down"
          description: >-
            Argus has failed {{{{ $labels.kind }}}} {{{{ $labels.target }}}} three times
            in a row. {base}/#/check/{{{{ $labels.check }}}}

      - alert: ArgusHeartbeatLate
        expr: |
          (time() - argus_heartbeat_last_seen_timestamp_seconds) > argus_heartbeat_max_age_seconds
            or (argus_heartbeat_max_age_seconds unless argus_heartbeat_last_seen_timestamp_seconds)
        for: 5m
        annotations:
          summary: "{{{{ $labels.heartbeat }}}} has gone quiet"
          description: "The timer behind it has stopped or is failing. {base}/"

      - alert: ArgusJobFailed
        expr: argus_job_last_result == 0
        for: 1m
        annotations:
          summary: "{{{{ $labels.job_name }}}} failed"
          description: "Its newest run reported a failure. {base}/"

      - alert: ArgusJobLate
        expr: (time() - argus_job_last_run_timestamp_seconds) > argus_job_max_age_seconds
        for: 30m
        annotations:
          summary: "{{{{ $labels.job_name }}}} has not run on schedule"
          description: "No report for longer than its limit. {base}/"

      - alert: ArgusCompanionQuiet
        expr: time() - argus_companion_last_report_timestamp_seconds > {QUIET_AFTER}
        for: 1m
        labels: {{ severity: critical }}
        annotations:
          summary: "host {{{{ $labels.companion }}}} has gone quiet"
          description: >-
            Its companion has stopped reporting: the host, its network or the
            companion is down. {base}/#/hosts

      - alert: ArgusDiskFull
        expr: argus_companion_disk_used_ratio >= {DISK_FULL}
        for: 5m
        labels: {{ severity: critical }}
        annotations:
          summary: "{{{{ $labels.mount }}}} on {{{{ $labels.companion }}}} is almost full"
          description: "{{{{ $value | humanizePercentage }}}} used. {base}/#/hosts"

      - alert: ArgusConfigRejected
        expr: argus_config_last_reload_success == 0
        for: 10m
        labels: {{ severity: warning }}
        annotations:
          summary: "Argus rejected the newest argus.yml"
          description: "The previous config is still running. Argus's log names the error."

      - alert: ArgusTestAlert
        expr: argus_alert_test == 1
        labels: {{ severity: critical }}
        annotations:
          summary: "Test from Argus: Prometheus alerts reach you"
          description: "Started from Argus's Alerts page. It clears by itself in 15 minutes."
"""


ALERTMANAGER_EXAMPLE = """# Alertmanager: page critical alerts to Telegram. Put the bot token in a
# file Alertmanager can read; never in this config.
route:
  receiver: "null"   # warnings are for the dashboard, not your phone
  routes:
    - matchers: [severity="critical"]
      receiver: telegram
      repeat_interval: 4h
receivers:
  - name: "null"
  - name: telegram
    telegram_configs:
      - bot_token_file: /etc/alertmanager/telegram_token
        chat_id: 123456789   # your chat ID
        parse_mode: ""
"""
