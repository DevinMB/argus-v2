package main

import (
	"fmt"
	"os"
	"strings"
	"time"
)

// Uninstalling the companion (design decision 18). Two steps, so Argus hears
// how the first went before the second removes the program that would tell it:
//
//  1. handBack: every stack Argus created (in /var/lib/argus-companion/stacks)
//     moves to /opt/stacks/<name>, compose file and .env together, so it stays
//     a plain Compose folder its owner can run. Running containers are left
//     alone. Stacks Argus took over already live in their own folders.
//  2. removal: the service, the binary, /etc/argus-companion and the state
//     folder. The service stops last: stopping it may end this very script.
//
// ARGUS_ROOT prefixes every path, for tests only.

const handBackScript = `set -u
R="${ARGUS_ROOT:-}"
src="$R/var/lib/argus-companion/stacks"
if [ ! -d "$src" ] || [ -z "$(ls -A "$src" 2>/dev/null)" ]; then
  echo "no stacks Argus created on this host"
  exit 0
fi
mkdir -p "$R/opt/stacks"
for d in "$src"/*/; do
  [ -d "$d" ] || continue
  n=$(basename "$d")
  dst="$R/opt/stacks/$n"
  [ -e "$dst" ] && dst="$R/opt/stacks/$n-argus"
  if [ -e "$dst" ]; then
    echo "kept $n in $src: $R/opt/stacks/$n and $dst both exist"
    continue
  fi
  if mv "${d%/}" "$dst"; then echo "handed back $n: ${dst#$R}"; else echo "could not move $n"; fi
done
`

const removalScript = `set -u
R="${ARGUS_ROOT:-}"
rm -f "$R/usr/local/bin/argus-companion"
rm -rf "$R/etc/argus-companion"
state="$R/var/lib/argus-companion"
if [ -n "$(ls -A "$state/stacks" 2>/dev/null)" ]; then
  # A stack that could not be handed back stays where it is, never deleted.
  for x in "$state"/* "$state"/.[!.]*; do
    [ -e "$x" ] && [ "$x" != "$state/stacks" ] && rm -rf "$x"
  done
  echo "left $state/stacks: move what is in it yourself"
else
  rm -rf "$state"
fi
if [ -d "$R/run/systemd/system" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl disable argus-companion >/dev/null 2>&1
  rm -f "$R/etc/systemd/system/argus-companion.service"
  systemctl daemon-reload
  echo "argus-companion is uninstalled"
  systemctl stop argus-companion >/dev/null 2>&1
  systemctl reset-failed argus-companion >/dev/null 2>&1
  exit 0
fi
if [ -f "$R/etc/init.d/argus-companion" ] && command -v rc-update >/dev/null 2>&1; then
  rc-update del argus-companion default >/dev/null 2>&1
  echo "argus-companion is uninstalled"
  # Detached: stopping the service must not end the step that removes it.
  setsid sh -c 'sleep 1; rc-service argus-companion stop >/dev/null 2>&1; rm -f /etc/init.d/argus-companion' \
    </dev/null >/dev/null 2>&1 &
  exit 0
fi
rm -f "$R/etc/init.d/argus-companion" "$R/etc/systemd/system/argus-companion.service"
echo "argus-companion is uninstalled; stop the running one with your init system"
`

// UninstallResult is how the handback went, sent before the removal.
type UninstallResult struct {
	State string `json:"state"` // ok or failed
	Log   string `json:"log"`
}

// Uninstaller holds the handback's result until a report carries it, and
// whether that report went through, which starts the removal.
type Uninstaller struct {
	run    func(timeout time.Duration, env []string, name string, args ...string) (string, error)
	result *UninstallResult
	sent   bool
}

func newUninstaller() *Uninstaller { return &Uninstaller{run: runOutside} }

// HandBack moves the stacks out, outside the read-only sandbox.
func (u *Uninstaller) HandBack() {
	if u.result != nil {
		return
	}
	out, err := u.run(5*time.Minute, nil, "sh", "-c", handBackScript)
	r := &UninstallResult{State: "ok", Log: strings.TrimSpace(out)}
	if err != nil {
		r.State, r.Log = "failed", strings.TrimSpace(out+"\n"+err.Error())
	}
	u.result = r
}

// Take is the result for the next report, until one has carried it.
func (u *Uninstaller) Take() *UninstallResult {
	if u.result == nil || u.sent {
		return nil
	}
	return u.result
}

// Sent marks the result delivered; true when the removal should start.
func (u *Uninstaller) Sent() bool {
	if u.result == nil || u.sent {
		return false
	}
	u.sent = true
	return u.result.State == "ok"
}

// Remove takes the companion off this host. It usually does not return: the
// service stops at the end.
func (u *Uninstaller) Remove() (string, error) {
	return u.run(5*time.Minute, nil, "sh", "-c", removalScript)
}

// uninstallHere is `argus-companion uninstall`, run by hand as root: for a host
// Argus cannot reach, or after Remove in Argus.
func uninstallHere() int {
	if os.Geteuid() != 0 {
		fmt.Fprintln(os.Stderr, "run this as root: sudo argus-companion uninstall")
		return 1
	}
	for _, s := range []string{handBackScript, removalScript} {
		out, err := runIn("/", nil, 5*time.Minute, "sh", "-c", s)
		fmt.Print(out)
		if err != nil {
			fmt.Fprintln(os.Stderr, "uninstall stopped:", err)
			return 1
		}
	}
	fmt.Println("If Argus still lists this host, press Remove on its page.")
	return 0
}
