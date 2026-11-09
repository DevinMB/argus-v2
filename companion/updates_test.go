package main

import (
	"bytes"
	"compress/gzip"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func TestAptDryRunNamesEachPackageItsVersionsAndSecurity(t *testing.T) {
	out := `NOTE: This is only a simulation!
Inst libssl3t64 [3.5.1-1] (3.5.1-1+deb13u1 Debian-Security:13/stable-security [amd64])
Inst tzdata [2025b-4] (2025b-4+deb13u1 Debian:13.2/stable [all])
Inst linux-image-new (6.12.48-1 Debian:13.2/stable [amd64])
Conf libssl3t64 (3.5.1-1+deb13u1 Debian-Security:13/stable-security [amd64])`
	got := parseAptPackages(out)
	want := []Package{
		{Name: "libssl3t64", From: "3.5.1-1", To: "3.5.1-1+deb13u1", Security: true},
		{Name: "linux-image-new", To: "6.12.48-1"},
		{Name: "tzdata", From: "2025b-4", To: "2025b-4+deb13u1"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v", got)
	}
}

func TestDnfAdvisoriesNameTheSecurityUpdatesAndTheirSeverity(t *testing.T) {
	pkgs := parseDnfPackages(`
openssl-libs.x86_64        1:3.2.2-6.el9_5.1     baseos
tzdata.noarch              2025b-1.el9           baseos
Obsoleting Packages
grub2-tools.x86_64         1:2.06-80.el9         baseos`)
	adv := parseDnfAdvisories(`RLSA-2026:1234 Important/Sec. openssl-libs-1:3.2.2-6.el9_5.1.x86_64
RLSA-2026:1240 Moderate/Sec.  openssl-libs-1:3.2.2-6.el9_5.1.x86_64`)
	got := withAdvisories(pkgs, adv)
	if len(got) != 2 || got[0].Name != "openssl-libs" || !got[0].Security || got[0].Severity != "Important" ||
		!reflect.DeepEqual(got[0].Advisories, []string{"RLSA-2026:1234", "RLSA-2026:1240"}) || got[1].Security {
		t.Fatalf("got %+v", got)
	}
	if nevraName("kernel-core-5.14.0-687.66.1.el9_8.x86_64") != "kernel-core" {
		t.Fatal("nevraName")
	}
}

func TestApkNamesItsUpgrades(t *testing.T) {
	got := parseApkPackages("busybox-1.36.1-r20 x86_64 {busybox} (GPL-2.0-only) [upgradable from: busybox-1.36.1-r19]")
	if len(got) != 1 || !reflect.DeepEqual(got[0], Package{Name: "busybox", From: "1.36.1-r19", To: "1.36.1-r20"}) {
		t.Fatalf("got %+v", got)
	}
}

func TestTheNewestChangelogEntrySaysWhatItFixes(t *testing.T) {
	cves, adv := changelogWhy(`openssl (3.5.1-1+deb13u1) trixie-security; urgency=medium

  * Fix CVE-2026-1001 and CVE-2026-1002 (DSA-6012-1).
  * CVE-2026-1001 again, said twice.

 -- Debian Security Team <team@security.debian.org>  Tue, 04 Nov 2026 10:00:00 +0000

openssl (3.5.1-1) unstable; urgency=medium

  * Fix CVE-2025-9999.
`)
	if !reflect.DeepEqual(cves, []string{"CVE-2026-1001", "CVE-2026-1002"}) || !reflect.DeepEqual(adv, []string{"DSA-6012-1"}) {
		t.Fatalf("cves %v advisories %v", cves, adv)
	}
}

// Changelogs download in the background, once per source and version; what is
// learned lands on the next look without a report ever waiting.
func TestChangelogsAreLearnedOnceInTheBackground(t *testing.T) {
	c := &Collector{run: func(string, ...string) (string, int, error) { return "openssl", 0, nil }}
	calls := 0
	cl := &Changelogs{known: map[string][2][]string{}, failed: map[string]bool{}, fetch: func(pkg, v string) (string, error) {
		calls++
		return "openssl (" + v + ") trixie-security\n  * CVE-2026-1001\n -- x\n", nil
	}}
	pkgs := []Package{{Name: "libssl3t64", To: "1+deb13u1", Security: true}, {Name: "openssl", To: "1+deb13u1", Security: true}, {Name: "tzdata", To: "2"}}
	todo := cl.fill(c, pkgs)
	if len(todo) != 2 {
		t.Fatalf("todo %+v", todo)
	}
	done := make(chan bool)
	cl.learn(c, todo, func() { done <- true })
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("never finished")
	}
	if calls != 1 {
		t.Fatalf("one source and version, one download; got %d", calls)
	}
	cl.fill(c, pkgs)
	if !reflect.DeepEqual(pkgs[0].CVEs, []string{"CVE-2026-1001"}) || pkgs[0].Source != "openssl" || pkgs[2].CVEs != nil {
		t.Fatalf("pkgs %+v", pkgs)
	}
}

// A security update whose changelog cannot be read says so, and one whose
// changelog names nothing says that, instead of promising an advisory.
func TestChangelogsSayWhyNothingShows(t *testing.T) {
	c := &Collector{run: func(string, ...string) (string, int, error) { return "", 1, nil }}
	fail := true
	cl := &Changelogs{known: map[string][2][]string{}, failed: map[string]bool{}, fetch: func(pkg, v string) (string, error) {
		if fail {
			return "", errors.New("404")
		}
		return pkg + " (" + v + ") trixie-security\n  * Fix a crash.\n -- x\n", nil
	}}
	pkgs := []Package{{Name: "vim-common", To: "2+deb13u2", Security: true}}
	learn := func() {
		done := make(chan bool)
		cl.learn(c, cl.fill(c, pkgs), func() { done <- true })
		<-done
		cl.fill(c, pkgs)
	}
	learn()
	if pkgs[0].Why != "unavailable" {
		t.Fatalf("after a failed read: %+v", pkgs[0])
	}
	fail = false
	learn()
	if pkgs[0].Why != "none" {
		t.Fatalf("after a read naming nothing: %+v", pkgs[0])
	}
}

// The changelog comes out of an unpacked package, even when its doc folder is
// another package's.
func TestReadDebChangelog(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "usr/share/doc/vim-common")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	z := gzip.NewWriter(&buf)
	z.Write([]byte("vim (2:9.1-2+deb13u2) trixie-security; urgency=medium\n\n  * CVE-2026-1234\n\n -- x\n"))
	z.Close()
	os.WriteFile(filepath.Join(dir, "changelog.Debian.gz"), buf.Bytes(), 0o644)
	out, err := readDebChangelog(root, "vim-tiny")
	if err != nil {
		t.Fatal(err)
	}
	if cves, _ := changelogWhy(out); !reflect.DeepEqual(cves, []string{"CVE-2026-1234"}) {
		t.Fatalf("cves %v", cves)
	}
	if n, ok := debSize("Package: vim\nSize: 1832\n"); !ok || n != 1832 {
		t.Fatalf("size %d %v", n, ok)
	}
}

// The row shows what the changelog entry says, so a security-suite update that
// names no CVE still has a reason.
func TestChangelogChanges(t *testing.T) {
	got := changelogChanges(`vim (2:9.1.1230-2+deb13u2) trixie-security; urgency=medium

  * Backport 9.2.0132 to fix test_recover.vim test failure on big-endian systems.
    (Closes: #1152930)
  * Fix CVE-2026-1.

 -- James McCoy <jamessan@debian.org>  Wed, 04 Nov 2026 17:12:34 -0500

vim (2:9.1.1230-2+deb13u1) trixie-security; urgency=medium

  * Fix CVE-2025-53905
`)
	want := "Backport 9.2.0132 to fix test_recover.vim test failure on big-endian systems. (Closes: #1152930)\nFix CVE-2026-1."
	if got != want {
		t.Fatalf("got %q", got)
	}
}
