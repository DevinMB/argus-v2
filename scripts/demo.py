"""Fill an Argus with a made-up homelab: hosts with containers and disks, checks,
a few timers and runs. For trying Argus out, and for the README's screenshots.

    python scripts/demo.py https://localhost:8443 --password 'your admin password'

It talks to Argus only through its API, as a companion and as the admin would;
the hosts it adds are not real and stop reporting when the script ends (run it
with --loop to keep them alive). Never point it at an Argus you care about: it
replaces the config.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import secrets
import ssl
import time
import urllib.error
import urllib.request

CONFIG = """\
# A made-up homelab, from scripts/demo.py.
groups:
  media: Jellyfin and the photo library
  home: the house itself
  network: DNS and the way in
checks:
  - {name: jellyfin, kind: tcp, target: "127.0.0.1:8096", group: media, severity: critical,
     companion: nas}
  - {name: immich, kind: tcp, target: "127.0.0.1:2283", group: media, companion: nas}
  - {name: paperless, kind: tcp, target: "127.0.0.1:8000", group: home, companion: nas}
  - {name: home-assistant, kind: tcp, target: "127.0.0.1:8123", group: home,
     severity: critical, companion: pi}
  - {name: pihole, kind: tcp, target: "127.0.0.1:53", group: network, severity: critical,
     companion: pi}
  - {name: reverse-proxy, kind: tcp, target: "127.0.0.1:443", group: network, companion: edge}
heartbeats:
  - {name: photo-sync, max_age: 3600, description: "Phone photos copied to the NAS"}
jobs:
  - {name: nas-backup, max_age: 691200, schedule: "Sundays 02:00", title: "Back up the NAS",
     steps: ["Snapshot the datasets", "Send them to the cloud bucket", "Check the copy"]}
  - {name: restore-test, max_age: 3024000, schedule: "Mondays 05:00",
     title: "Restore a sample", steps: ["Restore 50 random files", "Compare checksums"]}
"""

HOSTS = {
    "nas": {
        "os": "Debian GNU/Linux 13 (trixie)", "kernel": "6.12.48+deb13-amd64",
        "containers": [
            ("jellyfin", "jellyfin/jellyfin:10.11.1", "healthy", "media", [8096]),
            ("immich-server", "ghcr.io/immich-app/immich-server:v2.1.0", "healthy", "immich",
             [2283]),
            ("immich-db", "postgres:16.10", "healthy", "immich", []),
            ("paperless", "ghcr.io/paperless-ngx/paperless-ngx:2.19", "", "paperless", [8000]),
            ("syncthing", "syncthing/syncthing:2.0", "", "", [8384]),
        ],
        "disks": [("/", 64, 21), ("/srv", 7450, 5390)],
        "updates": (4, 1), "units": [],
    },
    "pi": {
        "os": "Raspbian GNU/Linux 12 (bookworm)", "kernel": "6.6.51+rpt-rpi-v8", "arch": "arm64",
        "containers": [
            ("pihole", "pihole/pihole:2025.10.2", "healthy", "dns", [53, 8080]),
            ("homeassistant", "ghcr.io/home-assistant/home-assistant:2025.10", "", "home",
             [8123]),
        ],
        "disks": [("/", 58, 31)],
        "updates": (0, 0), "units": [],
    },
    "edge": {
        "os": "Ubuntu 24.04.3 LTS", "kernel": "6.8.0-85-generic",
        "containers": [
            ("caddy", "caddy:2.10", "unhealthy", "proxy", [80, 443]),
            ("uptime-sidecar", "busybox:1.37", "", "", []),
        ],
        "disks": [("/", 30, 28.6)],
        "updates": (11, 3), "units": ["certbot.service"], "reboot": True,
    },
}
GB = 1024 ** 3
CHECKS_ON = {"nas": ["jellyfin", "immich", "paperless"], "pi": ["home-assistant", "pihole"],
             "edge": ["reverse-proxy"]}


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # a demo on localhost
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ""

    def call(self, method: str, path: str, body=None, headers=None):
        if not self.base.startswith(("https://", "http://")):
            raise SystemExit("the URL must start with https:// or http://")
        req = urllib.request.Request(self.base + path, method=method,  # noqa: S310
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              "X-CSRF-Token": self.csrf, **(headers or {})})
        try:
            with self.opener.open(req, timeout=20) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read().decode()[:300]}") from None
        return json.loads(raw) if raw else None


def report(c: Client, token: str, name: str, h: dict) -> None:
    containers = [{"name": n, "image": img, "state": "running", "health": health,
                   "status": "Up 3 days" + (f" ({health})" if health else ""), "project": proj,
                   "ports": ports, "working_dir": f"/srv/stacks/{proj}" if proj else "",
                   "config_files": [f"/srv/stacks/{proj}/compose.yml"] if proj else []}
                  for n, img, health, proj, ports in h["containers"]]
    pending, security = h["updates"]
    now = time.localtime()
    c.call("POST", "/companion/report", {
        "nonce": secrets.token_urlsafe(16), "version": "1.0.0+demo", "hostname": name,
        "os": h["os"], "kernel": h["kernel"], "arch": h.get("arch", "amd64"),
        "uptime": 3 * 86400, "containers": containers, "failed_units": h["units"],
        "disks": [{"mount": m, "total": int(t * GB), "used": int(u * GB)}
                  for m, t, u in h["disks"]],
        "updates": {"pending": pending, "security": security, "manager": "apt",
                    "checked_at": time.time()},
        "reboot_required": h.get("reboot", False), "policy_roots": ["/srv", "/opt"],
        "local": {"weekday": (now.tm_wday), "time": time.strftime("%H:%M", now),
                  "date": time.strftime("%Y-%m-%d", now)},
        # The checks this host runs: all answer, except the edge's proxy.
        "checks": [{"name": n, "ok": name != "edge", "duration": 0.004,
                    "error": "" if name != "edge" else "connection refused"}
                   for n in CHECKS_ON.get(name, [])],
    }, {"Authorization": f"Bearer {token}"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("--password", required=True)
    ap.add_argument("--setup-code", help="for a fresh install: the code from Argus's log")
    ap.add_argument("--loop", action="store_true", help="keep the hosts reporting")
    a = ap.parse_args()
    c = Client(a.url)
    if a.setup_code:
        out = c.call("POST", "/api/setup", {"code": a.setup_code, "password": a.password})
        c.csrf = out["csrf"]
    else:
        c.csrf = c.call("POST", "/api/login", {"password": a.password})["csrf"]
    base = c.call("GET", "/api/config")["base"]
    c.call("PUT", "/api/config", {"text": CONFIG, "base": base})

    tokens = {}
    for name in HOSTS:
        code = c.call("POST", "/api/companions/codes", {"url": a.url, "name": name})["code"]
        tokens[name] = c.call("POST", "/companion/enroll", {"code": code, "hostname": name})[
            "token"]
    c.call("PUT", "/api/patching/nas", {
        "enabled": True, "day": 1, "time": "03:00",
        "backup": "job:nas-backup", "restore": "job:restore-test"})
    c.call("PUT", "/api/patching/pi", {"enabled": True, "day": 3, "time": "04:00",
                                       "level": "security", "reboot": False, "no_backup": True})
    now = time.time()
    c.call("POST", "/job/nas-backup", {
        "result": "ok", "started": now - 2 * 86400 - 1500, "finished": now - 2 * 86400,
        "summary": "3 datasets, 412 GB sent", "items": [
            {"name": "photos", "result": "ok"}, {"name": "documents", "result": "ok"},
            {"name": "media", "result": "ok"}]})
    c.call("POST", "/job/restore-test", {
        "result": "ok", "started": now - 4 * 86400 - 300, "finished": now - 4 * 86400,
        "summary": "50 of 50 files restored and matching"})
    c.call("POST", "/heartbeat/photo-sync")
    # Take (and drop) an export, as a careful owner would; it quiets the reminder.
    c.call("POST", "/api/export", {"password": a.password, "passphrase": secrets.token_hex(16)})
    while True:
        for name, h in HOSTS.items():
            report(c, tokens[name], name, h)
        if not a.loop:
            break
        time.sleep(20)
    print(f"Demo homelab in place at {a.url}: {len(HOSTS)} hosts, "
          f"{CONFIG.count('{name:')} checks and runs.")


if __name__ == "__main__":
    main()
