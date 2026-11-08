package main

import (
	"encoding/json"
	"errors"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// GuestInstall is Argus asking this Proxmox server to add the companion inside
// one of its guests. Only fields come from Argus; the command is built here,
// and each field is checked so nothing in it can reach a shell as code.
type GuestInstall struct {
	VMID int    `json:"vmid"`
	Type string `json:"type"` // qemu or lxc
	URL  string `json:"url"`
	Pin  string `json:"pin"`
	Code string `json:"code"` // the request's id; with Reinstall, not sent to the guest
	// Replace the companion already in the guest, keeping its enrollment (the
	// install command without a code): Reinstall companion on a host's page.
	Reinstall bool `json:"reinstall,omitempty"`
}

type GuestResult struct {
	VMID   int    `json:"vmid"`
	Code   string `json:"code"`
	OK     bool   `json:"ok"`
	Output string `json:"output"`
}

var (
	guestURL  = regexp.MustCompile(`^https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~/-]*)?$`)
	guestPin  = regexp.MustCompile(`^[A-Za-z0-9+/]{43}=$`)
	guestCode = regexp.MustCompile(`^[a-z2-7]{4}(-[a-z2-7]{4}){3}$`)
)

const guestTimeout = 10 * time.Minute

func (g GuestInstall) check() error {
	switch {
	case g.VMID < 1 || g.VMID > 999999999:
		return errors.New("not a usable VMID")
	case g.Type != "qemu" && g.Type != "lxc":
		return errors.New("not a qemu or lxc guest")
	case !guestURL.MatchString(g.URL):
		return errors.New("not a usable Argus address")
	case g.Pin != "" && !guestPin.MatchString(g.Pin):
		return errors.New("not a usable key pin")
	case g.Pin == "" && strings.HasPrefix(g.URL, "https://"):
		return errors.New("an https address needs a key pin")
	case !guestCode.MatchString(g.Code):
		return errors.New("not a usable code")
	}
	return nil
}

// script is what runs inside the guest, as root: curl if it is missing, then
// the same install command Add a host shows, without sudo.
func (g GuestInstall) script() string {
	fetch, args := "curl -fsS", "--url "+g.URL
	if g.Pin != "" {
		fetch = "curl -fsS -k --pinnedpubkey sha256//" + g.Pin
		args += " --pin " + g.Pin
	}
	return strings.Join([]string{
		"set -e",
		"if ! command -v curl >/dev/null 2>&1; then",
		"  if command -v apk >/dev/null 2>&1; then apk add --no-cache -q curl",
		"  elif command -v apt-get >/dev/null 2>&1; then apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl",
		"  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q curl",
		"  else echo 'no curl and no apk, apt-get or dnf to install it' >&2; exit 1; fi",
		"fi",
		fetch + " " + g.URL + "/companion/install.sh | sh -s -- " + args + codeArg(g),
	}, "\n")
}

func codeArg(g GuestInstall) string {
	if g.Reinstall {
		return "" // keeps the guest's enrollment
	}
	return " --code " + g.Code
}

// command is how this server runs the script in the guest.
func (g GuestInstall) command() (string, []string) {
	id := strconv.Itoa(g.VMID)
	if g.Type == "lxc" {
		return "pct", []string{"exec", id, "--", "sh", "-c", g.script()}
	}
	return "qm", []string{"guest", "exec", id, "--timeout", strconv.Itoa(int(guestTimeout.Seconds())),
		"--", "sh", "-c", g.script()}
}

// qmResult reads `qm guest exec`'s JSON: the guest's exit code and output.
func qmResult(out string) (bool, string) {
	var r struct {
		ExitCode int    `json:"exitcode"`
		Exited   int    `json:"exited"`
		Out      string `json:"out-data"`
		Err      string `json:"err-data"`
	}
	if i := strings.Index(out, "{"); i < 0 || json.Unmarshal([]byte(out[i:]), &r) != nil {
		return false, out
	}
	text := strings.TrimSpace(r.Out + "\n" + r.Err)
	if r.Exited != 1 {
		return false, "still running in the guest after " + guestTimeout.String() + "\n" + text
	}
	return r.ExitCode == 0, text
}

// GuestInstaller runs the requests one at a time and keeps each code's result
// until Argus has had it, so a request repeated in later replies runs once.
type GuestInstaller struct {
	run  func(timeout time.Duration, env []string, name string, args ...string) (string, error)
	mu   sync.Mutex
	seen map[string]bool
	done []GuestResult
	busy bool
}

func newGuestInstaller() *GuestInstaller {
	return &GuestInstaller{run: runOutside, seen: map[string]bool{}}
}

func (gi *GuestInstaller) Start(reqs []GuestInstall) {
	gi.mu.Lock()
	var todo []GuestInstall
	for _, g := range reqs {
		if gi.seen[g.Code] {
			continue
		}
		gi.seen[g.Code] = true
		if err := g.check(); err != nil {
			gi.done = append(gi.done, GuestResult{VMID: g.VMID, Code: g.Code, Output: "refused: " + err.Error()})
			continue
		}
		todo = append(todo, g)
	}
	if gi.busy || len(todo) == 0 {
		gi.mu.Unlock()
		if len(todo) > 0 {
			gi.forget(todo) // picked up again from the next reply
		}
		return
	}
	gi.busy = true
	gi.mu.Unlock()
	go func() {
		for _, g := range todo {
			gi.install(g)
		}
		gi.mu.Lock()
		gi.busy = false
		gi.mu.Unlock()
	}()
}

func (gi *GuestInstaller) forget(gs []GuestInstall) {
	gi.mu.Lock()
	defer gi.mu.Unlock()
	for _, g := range gs {
		delete(gi.seen, g.Code)
	}
}

func (gi *GuestInstaller) install(g GuestInstall) {
	name, args := g.command()
	out, err := gi.run(guestTimeout, nil, name, args...)
	ok := err == nil
	if g.Type == "qemu" && err == nil {
		ok, out = qmResult(out)
	}
	out = strings.TrimSpace(out)
	if err != nil {
		out = strings.TrimSpace(out + "\n" + err.Error())
		if g.Type == "qemu" && strings.Contains(out, "agent") {
			out += "\nThis VM's QEMU guest agent is not running: install qemu-guest-agent in it and turn the agent on in Proxmox (Options), or run Add a host in the VM."
		}
	}
	if len(out) > 4000 {
		out = out[len(out)-4000:]
	}
	gi.mu.Lock()
	gi.done = append(gi.done, GuestResult{VMID: g.VMID, Code: g.Code, OK: ok, Output: out})
	gi.mu.Unlock()
}

// Take hands over the finished results for the next report.
func (gi *GuestInstaller) Take() []GuestResult {
	gi.mu.Lock()
	defer gi.mu.Unlock()
	out := gi.done
	gi.done = nil
	return out
}

func (gi *GuestInstaller) Busy() bool {
	gi.mu.Lock()
	defer gi.mu.Unlock()
	return gi.busy
}
