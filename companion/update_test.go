package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestUpdateInstallsOnlyTheBuildArgusSigned(t *testing.T) {
	dir := t.TempDir()
	exe := filepath.Join(dir, "argus-companion")
	write(t, exe, "old build")
	served := "new build"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, "/companion/download/linux-") {
			http.NotFound(w, r)
			return
		}
		w.Write([]byte(served))
	}))
	defer srv.Close()
	var installed []string
	u := &Updater{Exe: exe, Self: fileSHA256(exe), StateDir: dir, URL: srv.URL, client: srv.Client(),
		tried: map[string]time.Time{},
		run: func(_ time.Duration, _ []string, name string, args ...string) (string, error) {
			installed = append(installed, name+" "+strings.Join(args, " "))
			if name == "mv" {
				return "", os.Rename(args[1], args[2])
			}
			b, _ := os.ReadFile(args[2])
			return "", os.WriteFile(args[3], b, 0o755)
		}}
	sum := sha256.Sum256([]byte("new build"))
	want := hex.EncodeToString(sum[:])

	if u.Due(u.Self) || u.Due("") || !u.Due(want) {
		t.Fatal("Due: only a different build is due")
	}
	// A file that is not the signed build is never installed.
	served = "tampered"
	if err := u.Update(want); err == nil || !strings.Contains(err.Error(), "not the") {
		t.Fatalf("tampered: %v", err)
	}
	if b, _ := os.ReadFile(exe); string(b) != "old build" || len(installed) != 0 {
		t.Fatal("a wrong download was installed")
	}
	if u.Due(want) {
		t.Fatal("a failed build is tried again only after an hour")
	}
	served, u.tried = "new build", map[string]time.Time{}
	if err := u.Update(want); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(exe); string(b) != "new build" || len(installed) != 2 ||
		!strings.HasSuffix(installed[0], exe+".new") || installed[1] != "mv -f "+exe+".new "+exe {
		t.Fatalf("installed %q via %v", b, installed)
	}
	if left, _ := filepath.Glob(filepath.Join(dir, "*.new")); len(left) != 0 {
		t.Errorf("left behind: %v", left)
	}
}

func TestAPatchRunsFreshCountIsKept(t *testing.T) {
	c, none := newCollector(), 0
	c.SetUpdates(&Updates{Pending: &none, Manager: "apt"})
	if *c.updates.Pending != 0 || !c.updatesNext.After(time.Now().Add(updatesEvery-time.Minute)) {
		t.Fatalf("the count after a patch run must be kept until the next hourly look: %+v %v", c.updates, c.updatesNext)
	}
}

func TestAnExpiredDnfCacheIsRefreshedAndARealErrorShown(t *testing.T) {
	if !has("dnf") {
		// pendingUpdates picks the package manager from the host; on a host
		// without dnf, check the pieces instead.
		if got := lastWords("Error: Failed to download metadata for repo 'appstream'\n\n"); got != "Error: Failed to download metadata for repo 'appstream'" {
			t.Fatal(got)
		}
		return
	}
	c := newCollector()
	c.run = func(string, ...string) (string, int, error) { return "", 1, nil }
	c.dnfFresh = func() (string, int, error) { return "pkg.x86_64 1.2 baseos\n", 100, nil }
	if u := c.pendingUpdates(); u.Error != "" || *u.Pending != 1 {
		t.Fatalf("%+v", u)
	}
	c.dnfFresh = func() (string, int, error) { return "Error: Failed to download metadata for repo 'appstream'", 1, nil }
	if u := c.pendingUpdates(); !strings.Contains(u.Error, "Failed to download metadata") {
		t.Fatalf("%+v", u)
	}
}
