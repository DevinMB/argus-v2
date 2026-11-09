#!/bin/sh
# Installs the Argus companion on this host. Served by Argus; the "Add a host"
# dialog gives the one command that runs it:
#
#   curl -fsS --pinnedpubkey sha256//PIN -k https://ARGUS/companion/install.sh \
#     | sudo sh -s -- --url https://ARGUS --pin PIN --code CODE [--read-only]
#
# It downloads the companion from the same Argus with the same pin, enrolls,
# and starts it as a systemd service (an OpenRC one on Alpine). Run it again to update the binary; it
# keeps an existing enrollment unless given a new --code.
set -eu

URL="" PIN="" CODE="" READ_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --pin) PIN="$2"; shift 2 ;;
    --code) CODE="$2"; shift 2 ;;
    --read-only) READ_ONLY="--read-only"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$URL" ] || { echo "--url is required" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "run this as root (sudo sh -s -- ...)" >&2; exit 1; }

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "no companion build for $(uname -m) yet" >&2; exit 1 ;;
esac

BIN=/usr/local/bin/argus-companion
CONF=/etc/argus-companion/companion.json
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

if [ -n "$PIN" ]; then
  # -k only skips the CA check; curl still refuses any key but the pinned one.
  curl -fsS -k --pinnedpubkey "sha256//$PIN" -o "$TMP" "$URL/companion/download/linux-$ARCH"
else
  curl -fsS -o "$TMP" "$URL/companion/download/linux-$ARCH"
fi
install -m 0755 "$TMP" "$BIN"
echo "installed $("$BIN" version)"

if [ -n "$CODE" ]; then
  if [ -n "$PIN" ]; then
    "$BIN" enroll --config "$CONF" --url "$URL" --pin "$PIN" --code "$CODE" $READ_ONLY
  else
    "$BIN" enroll --config "$CONF" --url "$URL" --code "$CODE" $READ_ONLY
  fi
elif [ ! -f "$CONF" ]; then
  echo "not enrolled: add --code from Argus's \"Add a host\"" >&2
  exit 1
fi

# What this host will never run, whatever Argus says: edit it here, on the host.
[ -f /etc/argus-companion/policy.json ] || "$BIN" policy > /etc/argus-companion/policy.json

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  # Deploys may write only where the host's policy lets stacks live; a "-" skips
  # a folder that does not exist. Re-run this script after changing the policy.
  RW=""
  for r in $("$BIN" policy-roots --policy /etc/argus-companion/policy.json); do RW="$RW -$r"; done
  cat > /etc/systemd/system/argus-companion.service <<UNIT
[Unit]
Description=Argus companion: reports this host to Argus
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/argus-companion run --config /etc/argus-companion/companion.json
Restart=always
RestartSec=10
# It writes its own folder (stacks it makes live in /var/lib/argus-companion) and,
# for stacks it deploys in place, the folders its policy allows.
StateDirectory=argus-companion
ReadWritePaths=$RW
StateDirectoryMode=0700
Environment=DOCKER_CONFIG=/var/lib/argus-companion/docker
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable argus-companion >/dev/null 2>&1
  systemctl restart argus-companion
  echo "argus-companion is running; this host appears in Argus within a minute."
  echo "To remove it later: Uninstall on its page in Argus, or sudo /usr/local/bin/argus-companion uninstall"
elif command -v openrc-run >/dev/null 2>&1 && [ -d /etc/init.d ]; then
  # Alpine and others on OpenRC: supervise-daemon restarts it like systemd would.
  mkdir -p /var/lib/argus-companion && chmod 0700 /var/lib/argus-companion
  cat > /etc/init.d/argus-companion <<'INIT'
#!/sbin/openrc-run
description="Argus companion: reports this host to Argus"
supervisor=supervise-daemon
command=/usr/local/bin/argus-companion
command_args="run --config /etc/argus-companion/companion.json"
respawn_delay=10
export DOCKER_CONFIG=/var/lib/argus-companion/docker

depend() {
  need net
}
INIT
  chmod 0755 /etc/init.d/argus-companion
  rc-update add argus-companion default >/dev/null 2>&1
  rc-service argus-companion restart >/dev/null 2>&1 || rc-service argus-companion start
  echo "argus-companion is running; this host appears in Argus within a minute."
else
  echo "No systemd or OpenRC here. Start it with your init system:"
  echo "  $BIN run --config $CONF"
fi
