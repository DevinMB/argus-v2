"""Adding Argus to a Proxmox guest from its server (design decision 14).

The owner presses Add Argus on a guest row. Argus makes a one-time code named
after the guest, and the server's companion, in its next signed reply, gets
the guest's VMID and type with the URL, key pin and code. The companion builds
the install command itself from those checked fields (never a command string
from Argus) and runs it in the guest: `pct exec` for a container, the QEMU
guest agent for a VM. It reports how it went in its next report.

Requests live in memory: a code lasts an hour (the server adds guests one at
a time), so a restart loses nothing worth keeping.
"""

from __future__ import annotations

from collections.abc import Callable

GIVE_UP = 60 * 60  # a request no report has answered by now has failed (its code is gone)
KEEP = 60 * 60  # how long a finished request shows on the page


class GuestInstalls:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.by_server: dict[str, dict[int, dict]] = {}

    def request(self, server: str, vmid: int, kind: str, name: str, url: str, pin: str | None,
                code: str, reinstall: bool = False) -> None:
        self.by_server.setdefault(server, {})[vmid] = {
            "vmid": vmid, "type": kind, "name": name, "url": url, "pin": pin or "",
            "code": code, "reinstall": reinstall, "state": "waiting", "output": "",
            "at": self.clock()}

    def _tidy(self, server: str) -> dict[int, dict]:
        now, reqs = self.clock(), self.by_server.get(server, {})
        for vmid, r in list(reqs.items()):
            if r["state"] in ("waiting", "running") and now - r["at"] > GIVE_UP:
                r.update(state="failed", output="the Proxmox server never answered; is "
                         "its companion up to date and reporting?", at=now)
            elif r["state"] in ("ok", "failed") and now - r["at"] > KEEP:
                del reqs[vmid]
        return reqs

    def to_send(self, server: str) -> list[dict]:
        out = []
        for r in self._tidy(server).values():
            if r["state"] == "waiting":
                out.append({k: r[k] for k in ("vmid", "type", "url", "pin", "code", "reinstall")})
                r["state"] = "running"
            elif r["state"] == "running":
                out.append({k: r[k] for k in ("vmid", "type", "url", "pin", "code", "reinstall")})
        return out

    def results(self, server: str, items: list[dict]) -> None:
        reqs = self.by_server.get(server, {})
        for it in items:
            r = reqs.get(it["vmid"])
            if r and r["code"] == it["code"] and r["state"] in ("waiting", "running"):
                out = it["output"][-2000:]
                if not it["ok"] and "install: not found" in out:
                    out = ("This guest lacks the basic tools a companion needs: an appliance "
                           "such as Home Assistant OS, which updates itself. Skip it.\n" + out)
                r.update(state="ok" if it["ok"] else "failed", output=out, at=self.clock())

    def view(self, server: str) -> dict[str, dict]:
        return {str(v): {k: r[k] for k in ("state", "output", "at", "name", "reinstall")}
                for v, r in self._tidy(server).items()}

    def rename(self, old: str, new: str) -> None:
        if old in self.by_server:
            self.by_server[new] = self.by_server.pop(old)

    def forget(self, server: str) -> None:
        self.by_server.pop(server, None)
