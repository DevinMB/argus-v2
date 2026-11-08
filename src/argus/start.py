"""Getting started: the checklist on a new install, check suggestions from what
hosts run, and the coverage line that shows what to do next.

Everything here is worked out from what Argus already knows; nothing is
stored but whether the user hid the checklist.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from argus import sniff

HIDDEN = "start.hidden"
DONE = "start.done"  # the steps done once: they stay done


def _port_of(check) -> tuple[str, int] | None:
    """(host, port) a check probes, or None for ping."""
    if check.kind == "tcp":
        host, _, port = check.target.rpartition(":")
        return host.strip("[]"), int(port)
    if check.kind == "http":
        u = urlsplit(check.target)
        return u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
    return None


def _reachable(address: str) -> bool:
    """Whether Argus can probe the host at the address it reports from. Docker's
    bridge and loopback mean Argus shares the host: run the check inside it."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_loopback or ip in ipaddress.ip_network("172.16.0.0/12"))


def _name(container: str, port: int, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", container.lower()).strip("-")[:50] or "service"
    name = base if base not in taken else f"{base}-{port}"
    return name


def suggestions(companions: list[dict], checks, sniffer: sniff.Sniffer | None = None) -> list[dict]:
    """Each published port on a host, as a ready-to-add check, with whether a
    check already covers it. Apps first; plumbing (exporters, brokers) last,
    marked internal. A port the sniffer saw answer HTTP gets an HTTP check."""
    probed = {p for c in checks if (p := _port_of(c))}
    inside = {(c.companion, p[1]) for c in checks if c.companion and (p := _port_of(c))}
    taken = {c.name for c in checks}
    out = []
    for h in companions:
        rep = h.get("report") or {}
        addr = h.get("address", "")
        from_argus = _reachable(addr)
        for c in rep.get("containers") or []:
            if c.get("state") != "running":
                continue
            for port in c.get("ports") or []:
                covered = (addr, port) in probed or (h["name"], port) in inside or any(
                    host and port == p and host in (addr, h["name"], rep.get("hostname"))
                    for host, p in probed)
                name = _name(c["name"], port, taken)
                taken.add(name)
                # Published on the host's loopback only: only the host's companion reaches it.
                outside = from_argus and port not in (c.get("local_ports") or [])
                check = {"name": name, "kind": "tcp",
                         "target": f"{addr if outside else '127.0.0.1'}:{port}",
                         "group": c.get("project") or h["name"],
                         **({} if outside else {"companion": h["name"]})}
                out.append({
                    "host": h["name"], "container": c["name"], "port": port,
                    "covered": covered,
                    "internal": sniff.internal(c["name"], c.get("image", "")),
                    "check": sniff.shape(check, sniffer and sniffer.known(check["target"])),
                })
    # A Proxmox server's own web UI: Argus knows the server, so it is worth watching.
    for h in companions:
        rep = h.get("report") or {}
        addr = h.get("address", "")
        if not rep.get("proxmox") or not _reachable(addr):
            continue
        covered = (addr, 8006) in probed
        name = _name(f"pve-{h['name']}", 8006, taken)
        taken.add(name)
        out.append({"host": h["name"], "container": "Proxmox web UI", "port": 8006,
                    "covered": covered, "internal": False,
                    "check": {"name": name, "kind": "http", "target": f"https://{addr}:8006/",
                              "group": "infrastructure", "verify_tls": False}})
    if sniffer is not None:
        sniffer.kick([s["check"]["target"] for s in out
                      if not s["covered"] and s["check"]["kind"] == "tcp"
                      and not s["check"].get("companion")])
    return sorted(out, key=lambda s: s["internal"])


def view(*, rt, alerts: dict, gh_connected: bool, deploy_views: list[dict],
         companions: list[dict], hidden: bool, patched: int = 0,
         exported_at: float | None = None, sniffer: sniff.Sniffer | None = None,
         seen: set[str] | None = None) -> dict:
    sugg = suggestions(companions, rt.config.checks, sniffer)
    reporting = [h for h in companions if h["status"] == "ok"]
    stacks = [s for v in deploy_views for s in v["stacks"]]
    deployed = [s for s in stacks if s["status"] == "deployed"]
    steps = [
        {"key": "alerts", "done": alerts["sender"] == "prometheus" or bool(alerts.get("tested")),
         "title": "Make sure Argus can reach you",
         "why": "So you hear about a problem when it starts, not when someone tells you.",
         "href": "#/alerts", "action": "Set up alerts"},
        {"key": "host", "done": bool(reporting),
         "title": "Add your first host",
         "why": "One command; then Argus shows its containers, disks and updates.",
         "href": "#/hosts", "action": "Add a host"},
        {"key": "watcher", "done": bool(alerts.get("watched")),
         "title": "Know if Argus stops",
         "why": "A stopped Argus cannot tell you it stopped. Let a host on another machine "
                "watch it with its own Telegram bot, or name what already does.",
         "href": "#/alerts", "action": "Add a watcher"},
        {"key": "checks", "done": bool(rt.config.checks),
         "title": "Watch what matters",
         "why": "A check notices when a service stops answering, even when its host looks fine.",
         "href": "#", "action": "Add a check",
         "suggest": sum(1 for s in sugg if not s["covered"])},
        {"key": "patching", "done": patched > 0,
         "title": "Keep it patched",
         "why": "Link a host's backup and pick a weekly window; Argus patches one host "
                "at a time, only with a recent backup, and tells you how it went. On "
                "Proxmox, add Argus to the Proxmox server and each guest's backup is "
                "found for you. "
                "Already patched by something else? Say so on the host's card.",
         "href": "#/hosts?view=patching", "action": "Choose a window"},
        {"key": "github", "done": gh_connected,
         "title": "Connect GitHub",
         "why": "See each app's updates on their way: Dependabot's PRs, CI, merges and builds.",
         "href": "#/github", "action": "Connect GitHub"},
        {"key": "deploys", "done": bool(deployed),
         "title": "Deploy from git",
         "why": "Keep each host's stacks in a repo; a merge then deploys itself.",
         "href": "#/deploy", "action": "Set up deploys"},
    ]
    # A step once done stays done: it is a setup checklist, not a health meter, and
    # a moment with nothing deployed (Argus restarting, a host redeploying) must
    # not bring the setup bar back. Health has its own pages.
    for st in steps:
        st["done"] = st["done"] or st["key"] in (seen or set())
    services = sugg
    return {
        "hidden": hidden,
        # Argus's own data: a warning on the home page until an export is taken.
        "exported_at": exported_at,
        "steps": steps,
        "done": sum(s["done"] for s in steps),
        "suggestions": [s for s in sugg if not s["covered"]],
        "coverage": {
            "services": len(services), "watched": sum(s["covered"] for s in services),
            "hosts": len(companions), "reporting": len(reporting), "patched": patched,
            "stacks": len(stacks), "deployed": len(deployed),
        },
    }
