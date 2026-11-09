"""Uninstalling a host's companion from its page (design decision 18).

The owner confirms with the admin password twice. The host's next signed reply
asks its companion to uninstall; the companion hands the stacks Argus created
back as plain Compose folders, reports how that went, and removes itself. Only
on that report does Argus forget the host, so a host is never dropped while its
program is still running. The request is kept in settings: Argus may restart
in between. A request with no answer for 15 minutes is shown with the command
to run on the host instead.
"""

from __future__ import annotations

from collections.abc import Callable

from argus.settings import Settings

PENDING = "companion.uninstall"  # {cid: {"name", "at", "state", "log"}}
DONE = "companion.uninstalled"  # the newest finished ones, for the Hosts page
STALE = 15 * 60
KEEP = 10
COMMAND = "sudo /usr/local/bin/argus-companion uninstall"


class Uninstalls:
    def __init__(self, settings: Settings, clock: Callable[[], float]) -> None:
        self.settings, self.clock = settings, clock

    def _all(self) -> dict:
        return dict(self.settings.get(PENDING) or {})

    def request(self, cid: str, name: str) -> dict:
        req = {"name": name, "at": self.clock(), "state": "waiting", "log": ""}
        self.settings.set(PENDING, {**self._all(), cid: req})
        return self.view(cid)

    def cancel(self, cid: str) -> None:
        reqs = self._all()
        if reqs.pop(cid, None) is not None:
            self.settings.set(PENDING, reqs)

    def to_send(self, cid: str) -> bool:
        req = self._all().get(cid)
        return bool(req and req["state"] == "waiting")

    def result(self, cid: str, rep: dict | None) -> str | None:
        """The companion's handback result. "ok": forget the host now."""
        reqs = self._all()
        req = reqs.get(cid)
        if not rep or not req:
            return None
        log = str(rep.get("log", ""))[-4000:]
        if rep.get("state") == "ok":
            reqs.pop(cid)
            self.settings.set(PENDING, reqs)
            done = [{"name": req["name"], "at": self.clock(), "log": log}]
            self.settings.set(DONE, (done + list(self.settings.get(DONE) or []))[:KEEP])
            return "ok"
        reqs[cid] = {**req, "state": "failed", "log": log}
        self.settings.set(PENDING, reqs)
        return "failed"

    def view(self, cid: str) -> dict | None:
        req = self._all().get(cid)
        if not req:
            return None
        return {**req, "stale": req["state"] == "waiting" and self.clock() - req["at"] > STALE,
                "command": COMMAND}

    def done(self, since: float = 7 * 86400) -> list[dict]:
        return [d for d in self.settings.get(DONE) or [] if self.clock() - d["at"] < since]
