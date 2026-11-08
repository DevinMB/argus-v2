package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"
)

// PatchGrant is Argus's go-ahead, in its signed reply: one run, this far.
type PatchGrant struct {
	Run      int    `json:"run"`
	Level    string `json:"level"` // security or full
	Reboot   bool   `json:"reboot"`
	Snapshot bool   `json:"snapshot"`
}

// PatchState is what the report says about the newest run. It is kept on disk
// so a run survives the reboot it asked for.
type PatchState struct {
	Run     int      `json:"run"`
	State   string   `json:"state"` // running, rebooting, ok, failed
	Summary string   `json:"summary,omitempty"`
	Log     string   `json:"log,omitempty"`
	At      float64  `json:"at"`
	Before  []string `json:"before,omitempty"` // containers running before, with health
}

type LocalTime struct {
	Weekday int    `json:"weekday"` // Monday is 0
	Time    string `json:"time"`
	Date    string `json:"date"`
	Zone    string `json:"zone"`
}

func localNow(t time.Time) LocalTime {
	zone, _ := t.Zone()
	return LocalTime{Weekday: (int(t.Weekday()) + 6) % 7, Time: t.Format("15:04"),
		Date: t.Format("2006-01-02"), Zone: zone}
}

// Patcher runs the package manager outside the companion's own sandbox, in a
// transient systemd unit, so the service that runs all the time never needs
// write access to the system.
type Patcher struct {
	StateDir  string
	collector *Collector
	run       func(timeout time.Duration, env []string, name string, args ...string) (string, error)
	reboot    func() error
	settle    time.Duration // how long containers get to come back
	pkgBusy   func() bool   // a package manager is at work on the host
	pkgWait   time.Duration // how often to look again while one is
	// A run is going on in this process now. Busy is this, not the record on
	// disk: a record left "running" by a restart must never block patching or
	// self-updates (argus's companion was stuck that way, 2026-11-07).
	active atomic.Bool
}

func newPatcher(stateDir string, c *Collector) *Patcher {
	return &Patcher{StateDir: stateDir, collector: c, run: runOutside, settle: 5 * time.Minute,
		reboot: rebootHost, pkgBusy: func() bool { return pkgBusyProc("/proc") }, pkgWait: 10 * time.Second}
}

// runOutside runs a command outside the service's read-only sandbox: in a
// transient unit when systemd is there; when systemd refuses (Rocky 9's does
// for this sandboxed service: "Failed to start transient service unit:
// Connection reset by peer"), in the host's own mount namespace, PID 1's.
func runOutside(timeout time.Duration, env []string, name string, args ...string) (string, error) {
	if _, err := exec.LookPath("systemd-run"); err == nil {
		if _, err := os.Stat("/run/systemd/system"); err == nil {
			full := []string{"--wait", "--pipe", "--collect", "--quiet", "--service-type=exec",
				fmt.Sprintf("--property=RuntimeMaxSec=%d", int(timeout.Seconds()))}
			for _, e := range env {
				full = append(full, "--setenv="+e)
			}
			full = append(append(full, "--", name), systemdLiteral(args)...)
			out, err := runIn("/", nil, timeout+time.Minute, "systemd-run", full...)
			if err == nil || !strings.Contains(out, "Failed to start transient") {
				return out, err
			}
			if o, ok := runInHostMounts(timeout, env, name, args...); ok {
				return o.out, o.err
			}
			return out, err
		}
	}
	return runIn("/", env, timeout, name, args...) // OpenRC: no sandbox to leave
}

// systemdLiteral escapes $ for systemd-run, which expands ${NAME} in the
// command it starts: a script's own ${d} became "" (the first mars-b
// uninstall could not move its stack).
func systemdLiteral(args []string) []string {
	out := make([]string, len(args))
	for i, a := range args {
		out[i] = strings.ReplaceAll(a, "$", "$$")
	}
	return out
}

type result struct {
	out string
	err error
}

// runInHostMounts runs the command in PID 1's mount namespace, where the
// filesystem is writable as the host sees it. false: nsenter is not there.
func runInHostMounts(timeout time.Duration, env []string, name string, args ...string) (result, bool) {
	if _, err := exec.LookPath("nsenter"); err != nil {
		return result{}, false
	}
	full := append([]string{"--target", "1", "--mount", "--", "env"}, env...)
	full = append(append(full, name), args...)
	out, err := runIn("/", nil, timeout, "nsenter", full...)
	return result{out, err}, true
}

func (p *Patcher) path() string { return filepath.Join(p.StateDir, "patch.json") }

func (p *Patcher) State() *PatchState {
	b, err := os.ReadFile(p.path())
	if err != nil {
		return nil
	}
	var s PatchState
	if json.Unmarshal(b, &s) != nil {
		return nil
	}
	return &s
}

func (p *Patcher) save(s *PatchState) {
	s.At = float64(time.Now().Unix())
	s.Log = tail(s.Log, 32*1024)
	b, _ := json.MarshalIndent(s, "", "  ")
	_ = os.MkdirAll(p.StateDir, 0o700)
	_ = os.WriteFile(p.path(), b, 0o600)
}

// Busy says whether a run is under way here (so a second grant is ignored).
func (p *Patcher) Busy() bool {
	if p.active.Load() {
		return true
	}
	s := p.State()
	return s != nil && s.State == "rebooting" // the reboot this run asked for is pending
}

// healthy lists the containers running now as name or name+health.
func (p *Patcher) healthy() []string {
	var out []string
	cs, err := dockerContainers(p.collector.DockerSocket)
	if err != nil {
		return out
	}
	for _, c := range cs {
		if c.State == "running" && c.Health != "unhealthy" {
			out = append(out, c.Name+"/"+c.Health)
		}
	}
	return out
}

// missing: what ran before (healthy, if it has a health check) and does not now.
func missing(before, now []string) []string {
	have := map[string]string{}
	for _, n := range now {
		name, health, _ := strings.Cut(n, "/")
		have[name] = health
	}
	var out []string
	for _, b := range before {
		name, health, _ := strings.Cut(b, "/")
		got, ok := have[name]
		if !ok || (health == "healthy" && got != "healthy") {
			out = append(out, name)
		}
	}
	return out
}

func (p *Patcher) waitHealthy(before []string) []string {
	if _, err := os.Stat(p.collector.DockerSocket); err != nil {
		return nil
	}
	deadline := time.Now().Add(p.settle)
	for {
		gone := missing(before, p.healthy())
		if len(gone) == 0 || time.Now().After(deadline) {
			return gone
		}
		time.Sleep(5 * time.Second)
	}
}

// Start runs a grant to its end (or to the reboot it asks for).
func (p *Patcher) Start(g *PatchGrant) {
	p.active.Store(true)
	defer p.active.Store(false)
	s := &PatchState{Run: g.Run, State: "running", Before: p.healthy()}
	p.save(s)
	fail := func(why string) {
		s.State, s.Summary = "failed", why
		p.save(s)
	}

	if g.Snapshot {
		if ds := zfsRoot(p.collector.MountInfo); ds != "" {
			name := fmt.Sprintf("%s@argus-patch-%d", ds, g.Run)
			out, err := p.run(5*time.Minute, nil, "zfs", "snapshot", name)
			s.Log += "$ zfs snapshot " + name + "\n" + out
			if err != nil {
				fail("the snapshot before patching failed, so nothing was changed")
				return
			}
			s.Log += fmt.Sprintf("(to undo this run: zfs rollback %s, then reboot)\n", name)
		}
	}

	before := p.collector.pendingUpdates()
	steps, err := updateSteps(g.Level, p.collector)
	if err != nil {
		fail(err.Error())
		return
	}
	env := []string{"DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=a", "LC_ALL=C"}
	for _, st := range steps {
		out, err := p.run(time.Hour, env, st[0], st[1:]...)
		s.Log += "$ " + strings.Join(st, " ") + "\n" + out
		p.save(s)
		if err != nil {
			fail(fmt.Sprintf("%s failed: %v", st[0], err))
			return
		}
	}
	after := p.collector.pendingUpdates()
	p.collector.SetUpdates(after)
	s.Summary = summarize(g.Level, before, after)

	if gone := p.waitHealthy(s.Before); len(gone) > 0 {
		fail(s.Summary + "; not running again after it: " + strings.Join(gone, ", "))
		return
	}
	if r := p.collector.rebootRequired(); r != nil && *r {
		if !g.Reboot {
			s.State, s.Summary = "ok", s.Summary+"; it needs a reboot, which this host's safety net does not allow yet"
			p.save(s)
			return
		}
		s.State = "rebooting"
		p.save(s)
		if err := p.reboot(); err != nil {
			fail("the reboot did not start: " + err.Error())
		}
		return
	}
	s.State = "ok"
	p.save(s)
}

// AfterBoot finishes a run that rebooted: the same containers must be back.
func (p *Patcher) AfterBoot() {
	s := p.State()
	if s != nil && s.State == "running" {
		p.finishInterrupted(s)
		return
	}
	if s == nil || s.State != "rebooting" {
		return
	}
	if gone := p.waitHealthy(s.Before); len(gone) > 0 {
		s.State, s.Summary = "failed", s.Summary+"; after the reboot, not running: "+strings.Join(gone, ", ")
	} else {
		s.State, s.Summary = "ok", s.Summary+"; rebooted, everything came back"
	}
	p.save(s)
}

// finishInterrupted settles a run this program was in the middle of when it
// stopped (an upgrade can restart it): a just-started companion runs nothing,
// but the package manager it started outside its sandbox may still be at work.
// Once no package manager is left, what is still pending says how it went.
func (p *Patcher) finishInterrupted(s *PatchState) {
	p.active.Store(true) // settling counts as busy: no new run, no self-update meanwhile
	defer p.active.Store(false)
	for i := 0; i < 360 && p.pkgBusy(); i++ { // up to an hour at the usual pace
		time.Sleep(p.pkgWait)
	}
	u := p.collector.pendingUpdates()
	p.collector.SetUpdates(u)
	note := "the companion restarted during the run"
	switch {
	case u == nil || u.Error != "" || u.Pending == nil:
		s.State, s.Summary = "failed", note+"; what is still pending could not be read"
	case *u.Pending == 0:
		s.State, s.Summary = "ok", "all updates installed; "+note
	default:
		s.State, s.Summary = "failed", fmt.Sprintf("%s; %d update(s) still pending", note, *u.Pending)
	}
	if r := p.collector.rebootRequired(); r != nil && *r {
		s.Summary += "; it needs a reboot, left for its next window"
	}
	p.save(s)
}

// pkgBusyProc: whether a package manager is running, from /proc.
func pkgBusyProc(proc string) bool {
	names := map[string]bool{"apt": true, "apt-get": true, "dpkg": true, "dnf": true,
		"yum": true, "unattended-upgr": true, "apk": true}
	entries, _ := os.ReadDir(proc)
	for _, e := range entries {
		if b, err := os.ReadFile(filepath.Join(proc, e.Name(), "comm")); err == nil &&
			names[strings.TrimSpace(string(b))] {
			return true
		}
	}
	return false
}

// updateSteps are the commands for one run, by package manager and level.
func updateSteps(level string, c *Collector) ([][]string, error) {
	switch {
	case has("apt-get"):
		// Wait for the dpkg lock: a host's own daily apt job often holds it.
		lock := []string{"-o", "DPkg::Lock::Timeout=300"}
		steps := [][]string{append([]string{"apt-get", "update", "-q"}, lock...)}
		if level == "full" {
			return append(steps, []string{"apt-get", "-y", "-q", "-o", "DPkg::Lock::Timeout=300",
				"-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold", "dist-upgrade"}), nil
		}
		if has("unattended-upgrade") {
			return append(steps, []string{"unattended-upgrade", "-v"}), nil
		}
		// No unattended-upgrades: the packages apt would take from a -security suite.
		out, _, err := c.run("apt-get", "-s", "-o", "Debug::NoLocking=true", "dist-upgrade")
		if err != nil {
			return nil, err
		}
		pkgs := securityPackages(out)
		if len(pkgs) == 0 {
			return steps, nil
		}
		return append(steps, append([]string{"apt-get", "-y", "-q", "-o", "DPkg::Lock::Timeout=300",
			"-o", "Dpkg::Options::=--force-confold", "install", "--only-upgrade"}, pkgs...)), nil
	case has("dnf"):
		if level == "full" {
			return [][]string{{"dnf", "-y", "upgrade"}}, nil
		}
		return [][]string{{"dnf", "-y", "upgrade", "--security"}}, nil
	case has("apk"):
		if level != "full" {
			return nil, fmt.Errorf("Alpine has no security-only updates; this host needs a safety net that allows all updates")
		}
		return [][]string{{"apk", "update"}, {"apk", "upgrade"}}, nil
	}
	return nil, fmt.Errorf("no package manager Argus knows here (apt, dnf, apk)")
}

func securityPackages(aptSim string) []string {
	var out []string
	for _, line := range strings.Split(aptSim, "\n") {
		if strings.HasPrefix(line, "Inst ") && strings.Contains(line, "-security") {
			if f := strings.Fields(line); len(f) > 1 {
				out = append(out, f[1])
			}
		}
	}
	return out
}

func summarize(level string, before, after *Updates) string {
	what := map[string]string{"full": "all updates", "security": "security updates"}[level]
	if before == nil || after == nil || before.Pending == nil || after.Pending == nil {
		return what + " installed"
	}
	n := *before.Pending - *after.Pending
	return fmt.Sprintf("%s: %d package(s) updated, %d still pending", what, max(n, 0), *after.Pending)
}

// zfsRoot is the dataset mounted at /, or "" when / is not ZFS.
func zfsRoot(mountinfo string) string {
	b, err := os.ReadFile(mountinfo)
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(b), "\n") {
		pre, post, ok := strings.Cut(line, " - ")
		f, g := strings.Fields(pre), strings.Fields(post)
		if ok && len(f) >= 5 && f[4] == "/" && len(g) >= 2 && g[0] == "zfs" {
			return g[1]
		}
	}
	return ""
}

// rebootHost asks systemd to reboot, or the plain reboot command where there is
// no systemd (Alpine on OpenRC).
func rebootHost() error {
	if has("systemctl") {
		return exec.Command("systemctl", "reboot").Run()
	}
	return exec.Command("reboot").Run()
}
