package main

import (
	"errors"
	"strings"
	"testing"
	"time"
)

const testPin = "j9Y4u9ZL0fNYj8aVaUVBofT7QedblYwQkWMgu60JW8s="

func goodInstall() GuestInstall {
	return GuestInstall{VMID: 105, Type: "lxc", URL: "https://192.168.10.18:8443", Pin: testPin,
		Code: "abcd-efgh-ijkl-mn23"}
}

func TestGuestInstallFieldsCannotCarryShell(t *testing.T) {
	if err := goodInstall().check(); err != nil {
		t.Fatal(err)
	}
	bad := []func(*GuestInstall){
		func(g *GuestInstall) { g.URL = "https://x;reboot" },
		func(g *GuestInstall) { g.URL = "https://x/$(id)" },
		func(g *GuestInstall) { g.URL = "file:///etc/passwd" },
		func(g *GuestInstall) { g.Pin = testPin + " --code x" },
		func(g *GuestInstall) { g.Pin = "" },
		func(g *GuestInstall) { g.Code = "abcd-efgh-ijkl-mn23; rm -rf /" },
		func(g *GuestInstall) { g.Type = "host" },
		func(g *GuestInstall) { g.VMID = 0 },
	}
	for i, f := range bad {
		g := goodInstall()
		f(&g)
		if g.check() == nil {
			t.Errorf("case %d: %+v was accepted", i, g)
		}
	}
	plain := goodInstall()
	plain.URL, plain.Pin = "http://argus.lan:8080", ""
	if err := plain.check(); err != nil {
		t.Errorf("plain http without a pin: %v", err)
	}
}

func TestGuestInstallCommands(t *testing.T) {
	g := goodInstall()
	name, args := g.command()
	if name != "pct" || strings.Join(args[:4], " ") != "exec 105 -- sh" {
		t.Fatalf("lxc: %s %v", name, args)
	}
	s := args[len(args)-1]
	for _, want := range []string{"apk add", "apt-get install", "--pinnedpubkey sha256//" + testPin,
		"https://192.168.10.18:8443/companion/install.sh | sh -s -- --url https://192.168.10.18:8443 --pin " + testPin + " --code abcd-efgh-ijkl-mn23"} {
		if !strings.Contains(s, want) {
			t.Errorf("script lacks %q:\n%s", want, s)
		}
	}
	if strings.Contains(s, "sudo") {
		t.Error("the script runs as root in the guest; no sudo")
	}
	g.Type = "qemu"
	name, args = g.command()
	if name != "qm" || strings.Join(args[:6], " ") != "guest exec 105 --timeout 600 --" {
		t.Fatalf("qemu: %s %v", name, args)
	}
}

func TestQmResult(t *testing.T) {
	ok, out := qmResult(`{"exitcode":0,"exited":1,"out-data":"enrolled as couchdb\n"}`)
	if !ok || out != "enrolled as couchdb" {
		t.Errorf("%v %q", ok, out)
	}
	ok, out = qmResult(`{"exitcode":1,"exited":1,"err-data":"curl: (7) failed"}`)
	if ok || !strings.Contains(out, "curl: (7)") {
		t.Errorf("%v %q", ok, out)
	}
	if ok, _ = qmResult("garbage"); ok {
		t.Error("unreadable output counted as success")
	}
}

func TestGuestInstallerRunsEachCodeOnce(t *testing.T) {
	gi := newGuestInstaller()
	var ran []string
	gi.run = func(_ time.Duration, _ []string, name string, args ...string) (string, error) {
		ran = append(ran, name+" "+args[1])
		if args[1] == "106" {
			return "", errors.New("exit status 1")
		}
		return "enrolled", nil
	}
	a, b, bad := goodInstall(), goodInstall(), goodInstall()
	b.VMID, b.Code = 106, "bbbb-efgh-ijkl-mn23"
	bad.VMID, bad.Code = 107, "nope"
	gi.Start([]GuestInstall{a, b, bad})
	for gi.Busy() {
		time.Sleep(time.Millisecond)
	}
	gi.Start([]GuestInstall{a, b}) // Argus repeats them until it has the results
	for gi.Busy() {
		time.Sleep(time.Millisecond)
	}
	if strings.Join(ran, ",") != "pct 105,pct 106" {
		t.Errorf("ran %v", ran)
	}
	res := map[int]GuestResult{}
	for _, r := range gi.Take() {
		res[r.VMID] = r
	}
	if !res[105].OK || res[106].OK || res[107].OK || !strings.HasPrefix(res[107].Output, "refused") {
		t.Errorf("%+v", res)
	}
	if len(gi.Take()) != 0 {
		t.Error("results handed over twice")
	}
}

func TestAReinstallKeepsTheGuestsEnrollment(t *testing.T) {
	g := goodInstall()
	g.Reinstall = true
	if err := g.check(); err != nil {
		t.Fatal(err)
	}
	if s := g.script(); strings.Contains(s, "--code") || !strings.Contains(s, "install.sh | sh -s -- --url") {
		t.Fatalf("a reinstall must not enroll again:\n%s", s)
	}
}
