package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestSecurityPackagesAndSummaries(t *testing.T) {
	sim := `Inst libc6 [2.41-11] (2.41-12 Debian-Security:13/stable-security [amd64])
Inst curl [8.14.1-2] (8.14.1-3 Debian:13.2/stable [amd64])
Inst openssl [3.5.1-1] (3.5.4-1 Debian-Security:13/stable-security [amd64])`
	if got := securityPackages(sim); !reflect.DeepEqual(got, []string{"libc6", "openssl"}) {
		t.Fatalf("got %v", got)
	}
	n, z := 12, 1
	if s := summarize("security", &Updates{Pending: &n}, &Updates{Pending: &z}); s != "security updates: 11 package(s) updated, 1 still pending" {
		t.Fatal(s)
	}
	if got := missing([]string{"web/healthy", "db/", "job/"}, []string{"web/starting", "db/"}); !reflect.DeepEqual(got, []string{"web", "job"}) {
		t.Fatalf("missing %v", got)
	}
	if localNow(time.Date(2027, 1, 17, 12, 0, 0, 0, time.UTC)).Weekday != 6 {
		t.Fatal("Sunday must be 6 (Monday 0)")
	}
}

func TestZfsRoot(t *testing.T) {
	p := filepath.Join(t.TempDir(), "mountinfo")
	_ = os.WriteFile(p, []byte("22 1 0:21 / / rw - zfs rpool/ROOT/pve-1 rw\n23 22 0:22 / /data rw - zfs rpool/data rw\n"), 0o644)
	if got := zfsRoot(p); got != "rpool/ROOT/pve-1" {
		t.Fatal(got)
	}
}

func newTestPatcher(t *testing.T, rebootNeeded bool) (*Patcher, *[]string, *int) {
	dir := t.TempDir()
	c := newCollector()
	c.DockerSocket = filepath.Join(dir, "no-docker")
	c.MountInfo = filepath.Join(dir, "mountinfo")
	c.RebootFile = filepath.Join(dir, "reboot-required")
	if rebootNeeded {
		_ = os.WriteFile(c.RebootFile, nil, 0o644)
	}
	c.run = func(string, ...string) (string, int, error) { return "", 0, nil }
	var ran []string
	reboots := 0
	p := newPatcher(dir, c)
	p.run = func(_ time.Duration, env []string, name string, args ...string) (string, error) {
		ran = append(ran, name+" "+strings.Join(args, " "))
		return "ok\n", nil
	}
	p.reboot = func() error { reboots++; return nil }
	return p, &ran, &reboots
}

func TestARunPatchesAndOnlyRebootsWhenAllowed(t *testing.T) {
	if !has("apt-get") && !has("dnf") && !has("apk") {
		t.Skip("no package manager on this machine")
	}
	p, ran, reboots := newTestPatcher(t, true)
	p.Start(&PatchGrant{Run: 7, Level: "full", Reboot: false})
	s := p.State()
	if s.State != "ok" || !strings.Contains(s.Summary, "needs a reboot") || *reboots != 0 {
		t.Fatalf("no reboot allowed: %+v, reboots %d", s, *reboots)
	}
	if len(*ran) == 0 {
		t.Fatal("nothing ran")
	}
	p2, _, reboots2 := newTestPatcher(t, true)
	p2.Start(&PatchGrant{Run: 8, Level: "full", Reboot: true})
	if p2.State().State != "rebooting" || *reboots2 != 1 || !p2.Busy() {
		t.Fatalf("want a reboot: %+v", p2.State())
	}
	p2.AfterBoot()
	if s := p2.State(); s.State != "ok" || !strings.Contains(s.Summary, "rebooted") {
		t.Fatalf("after boot: %+v", s)
	}
}

func TestAFailedStepFailsTheRun(t *testing.T) {
	if !has("apt-get") && !has("dnf") && !has("apk") {
		t.Skip("no package manager on this machine")
	}
	p, _, _ := newTestPatcher(t, false)
	p.run = func(time.Duration, []string, string, ...string) (string, error) {
		return "E: dpkg was interrupted", os.ErrPermission
	}
	p.Start(&PatchGrant{Run: 9, Level: "full"})
	if s := p.State(); s.State != "failed" || !strings.Contains(s.Log, "dpkg was interrupted") {
		t.Fatalf("got %+v", s)
	}
}

func TestARunInterruptedByARestartIsSettledFromWhatIsPending(t *testing.T) {
	if !has("apt-get") && !has("dnf") && !has("apk") {
		t.Skip("no package manager on this machine")
	}
	// argus (CT 100): the upgrade finished, the companion restarted mid-run, and
	// its record said "running" for good, holding every other host's turn.
	p, _, _ := newTestPatcher(t, false)
	busy := 2
	p.pkgBusy, p.pkgWait = func() bool { busy--; return busy > 0 }, time.Millisecond
	p.save(&PatchState{Run: 16, State: "running", At: 1})
	p.AfterBoot()
	if s := p.State(); s.State != "ok" || !strings.Contains(s.Summary, "restarted during the run") {
		t.Fatalf("got %+v", s)
	}
	if pkgBusyProc(t.TempDir()) {
		t.Fatal("an empty /proc has no package manager")
	}
}

func TestAStaleRunningRecordDoesNotMakeTheCompanionBusy(t *testing.T) {
	// A record left "running" by a restart blocked patching and self-updates for good.
	p, _, _ := newTestPatcher(t, false)
	p.save(&PatchState{Run: 16, State: "running", At: 1})
	if p.Busy() {
		t.Fatal("a stale record on disk made the companion busy")
	}
	p.active.Store(true)
	if !p.Busy() {
		t.Fatal("a run in this process is busy")
	}
}
