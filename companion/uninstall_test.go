package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func runScript(t *testing.T, root, script string) string {
	t.Helper()
	cmd := exec.Command("sh", "-c", script)
	cmd.Env = append(os.Environ(), "ARGUS_ROOT="+root, "PATH=/usr/bin:/bin")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	return string(out)
}

func put(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

// The stacks Argus created come back as plain Compose folders, secrets
// included; then everything of the companion goes.
func TestUninstallHandsStacksBackThenRemovesEverything(t *testing.T) {
	root := t.TempDir()
	state := filepath.Join(root, "var/lib/argus-companion")
	put(t, filepath.Join(state, "stacks/web/compose.yml"), "services: {}")
	put(t, filepath.Join(state, "stacks/web/.env"), "PASSWORD=x")
	put(t, filepath.Join(state, "stacks/db/compose.yml"), "services: {}")
	put(t, filepath.Join(root, "opt/stacks/db/compose.yml"), "mine") // one already there
	put(t, filepath.Join(state, "deploys.json"), "{}")
	put(t, filepath.Join(root, "etc/argus-companion/companion.json"), "{}")
	put(t, filepath.Join(root, "usr/local/bin/argus-companion"), "bin")

	out := runScript(t, root, handBackScript)
	if !strings.Contains(out, "handed back web: /opt/stacks/web") || !strings.Contains(out, "handed back db: /opt/stacks/db-argus") || strings.Contains(out, root) {
		t.Fatalf("handback said: %s", out)
	}
	if b, _ := os.ReadFile(filepath.Join(root, "opt/stacks/web/.env")); string(b) != "PASSWORD=x" {
		t.Fatal("the .env did not come along")
	}
	if b, _ := os.ReadFile(filepath.Join(root, "opt/stacks/db/compose.yml")); string(b) != "mine" {
		t.Fatal("an existing folder was overwritten")
	}

	out = runScript(t, root, removalScript)
	for _, gone := range []string{state, filepath.Join(root, "etc/argus-companion"), filepath.Join(root, "usr/local/bin/argus-companion")} {
		if _, err := os.Stat(gone); !os.IsNotExist(err) {
			t.Fatalf("%s is still there; removal said: %s", gone, out)
		}
	}
	if !strings.Contains(out, "uninstalled") {
		t.Fatalf("removal said: %s", out)
	}
}

// A stack that could not be handed back is never deleted with the rest.
func TestUninstallKeepsAStackItCouldNotMove(t *testing.T) {
	root := t.TempDir()
	state := filepath.Join(root, "var/lib/argus-companion")
	put(t, filepath.Join(state, "stacks/web/compose.yml"), "argus")
	put(t, filepath.Join(root, "opt/stacks/web/compose.yml"), "mine")
	put(t, filepath.Join(root, "opt/stacks/web-argus/compose.yml"), "mine too")
	put(t, filepath.Join(state, "deploys.json"), "{}")

	cmd := exec.Command("sh", "-c", handBackScript)
	cmd.Env = append(os.Environ(), "ARGUS_ROOT="+root)
	b, err := cmd.CombinedOutput()
	if err == nil || !strings.Contains(string(b), "kept web") || !strings.Contains(string(b), "companion stays") {
		t.Fatalf("a stack left behind must fail the handback: %v %s", err, b)
	}
	out := runScript(t, root, removalScript)
	if b, _ := os.ReadFile(filepath.Join(state, "stacks/web/compose.yml")); string(b) != "argus" {
		t.Fatalf("the stack was deleted; removal said: %s", out)
	}
	if _, err := os.Stat(filepath.Join(state, "deploys.json")); !os.IsNotExist(err) {
		t.Fatal("the rest of the state stayed")
	}
}

func TestTheRemovalStartsOnlyAfterArgusHasTheHandback(t *testing.T) {
	u := &Uninstaller{run: func(_ time.Duration, _ []string, _ string, _ ...string) (string, error) {
		return "handed back web: /opt/stacks/web", nil
	}}
	if u.Take() != nil || u.Sent() {
		t.Fatal("nothing to send before a handback")
	}
	u.HandBack()
	if r := u.Take(); r == nil || r.State != "ok" {
		t.Fatalf("result %+v", r)
	}
	if !u.Sent() {
		t.Fatal("an ok handback, once sent, starts the removal")
	}
	if u.Take() != nil || u.Sent() {
		t.Fatal("sent once only")
	}
}

// What systemd-run gets back after its own $ expansion is the script as written.
func TestScriptsReachTheShellAsWrittenThroughSystemdRun(t *testing.T) {
	for _, s := range []string{handBackScript, removalScript} {
		if strings.Contains(s, "${") || strings.Contains(s, "%") {
			t.Fatal("no ${...} or % in a script run through systemd-run")
		}
		esc := systemdLiteral([]string{s})[0]
		if strings.ReplaceAll(esc, "$$", "$") != s {
			t.Fatal("systemd's $$ must give back every $")
		}
	}
}
