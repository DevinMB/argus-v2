package main

import (
	"io"
	"net"
	"net/http"
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

// Uninstall, reinstall, deploy: the stack runs from the folder it was handed
// back to, with no second copy, and keeps running from it after that.
func TestAReinstallDeploysWhereTheUninstallLeftTheStack(t *testing.T) {
	root := t.TempDir()
	state := filepath.Join(root, "var/lib/argus-companion")
	policy := filepath.Join(root, "policy.json")
	put(t, policy, `{"bind_roots": ["`+filepath.Join(root, "opt")+`"]}`)
	fresh := func() (*Deployer, *fakeCompose) {
		f := &fakeCompose{}
		d := newDeployer(state, policy)
		d.HandedBack = filepath.Join(root, "opt/stacks")
		d.compose = f.run
		d.DockerSocket = filepath.Join(root, "no-docker")
		return d, f
	}
	d, _ := fresh()
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})
	put(t, filepath.Join(d.StacksDir, "web/data"), "precious")

	out := runScript(t, root, handBackScript)
	home := filepath.Join(root, "opt/stacks/web")
	if b, _ := os.ReadFile(filepath.Join(home, ".argus-handed-back")); strings.TrimSpace(string(b)) != "web" {
		t.Fatalf("no mark in the handed-back folder; handback said: %s", out)
	}
	runScript(t, root, removalScript)

	d, f := fresh() // reinstalled: empty state
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d2", "v2")}})
	r := d.Results(&DeployPlan{Stacks: []StackSpec{stack("d2", "v2")}})[0]
	if r.Result != "ok" || !strings.Contains(r.Detail, "where the uninstall left it") {
		t.Fatalf("got %+v (compose calls %v)", r, f.calls)
	}
	if b, _ := os.ReadFile(filepath.Join(home, "compose.yml")); string(b) != "v2" {
		t.Fatalf("compose.yml in the handed-back folder = %q", b)
	}
	if b, _ := os.ReadFile(filepath.Join(home, "data")); string(b) != "precious" {
		t.Fatal("the stack's data was touched")
	}
	if _, err := os.Stat(filepath.Join(d.StacksDir, "web")); !os.IsNotExist(err) {
		t.Fatal("a second copy was made")
	}
	// The next deploy goes there too, and a failure rolls back to what ran.
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d3", "v3")}})
	if b, _ := os.ReadFile(filepath.Join(home, "compose.yml")); string(b) != "v3" {
		t.Fatalf("the next deploy did not go to the same folder: %q", b)
	}
	if _, err := os.Stat(filepath.Join(d.StacksDir, "web")); !os.IsNotExist(err) {
		t.Fatal("a second copy was made later")
	}
}

// The first deploy into a handed-back folder can roll back to the files it had.
func TestAFailedFirstDeployIntoAHandedBackFolderRollsBack(t *testing.T) {
	root := t.TempDir()
	policy := filepath.Join(root, "policy.json")
	put(t, policy, `{"bind_roots": ["`+filepath.Join(root, "opt")+`"]}`)
	home := filepath.Join(root, "opt/stacks/web")
	put(t, filepath.Join(home, "compose.yml"), "v1")
	put(t, filepath.Join(home, ".argus-handed-back"), "web\n")
	f := &fakeCompose{}
	d := newDeployer(filepath.Join(root, "state"), policy)
	d.HandedBack = filepath.Join(root, "opt/stacks")
	d.DockerSocket = filepath.Join(root, "no-docker")
	tries := 0
	d.compose = func(dir string, env []string, args ...string) (string, error) {
		if args[2] == "up" {
			tries++
			f.failUp = tries == 1
		}
		return f.run(dir, env, args...)
	}
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d2", "v2")}})
	r := d.Results(&DeployPlan{Stacks: []StackSpec{stack("d2", "v2")}})[0]
	if r.Result != "rolled_back" {
		t.Fatalf("got %+v", r)
	}
	if b, _ := os.ReadFile(filepath.Join(home, "compose.yml")); string(b) != "v1" {
		t.Fatalf("compose.yml = %q", b)
	}
	if _, err := os.Stat(filepath.Join(home, "conf/site.conf")); !os.IsNotExist(err) {
		t.Fatal("a file the old stack did not have was left behind")
	}
}

// A folder without the mark, or outside the policy, is never used.
func TestOnlyAMarkedAllowedFolderIsUsed(t *testing.T) {
	root := t.TempDir()
	policy := filepath.Join(root, "policy.json")
	put(t, policy, `{"bind_roots": ["/srv"]}`)
	put(t, filepath.Join(root, "opt/stacks/web/.argus-handed-back"), "web")
	put(t, filepath.Join(root, "opt/stacks/web/compose.yml"), "mine")
	d, _ := newTestDeployer(t)
	d.PolicyPath = policy
	d.HandedBack = filepath.Join(root, "opt/stacks")
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})
	if b, _ := os.ReadFile(filepath.Join(root, "opt/stacks/web/compose.yml")); string(b) != "mine" {
		t.Fatal("deployed into a folder the policy does not allow")
	}
	if _, err := os.Stat(filepath.Join(d.StacksDir, "web/compose.yml")); err != nil {
		t.Fatal("the usual folder was not used")
	}
}

// The running containers still name the old folder (moving it does not change
// their labels): that is the stack itself, not a second copy, so it deploys.
func TestContainersStillLabelledWithTheOldFolderDoNotBlock(t *testing.T) {
	root := t.TempDir()
	state := filepath.Join(root, "state")
	policy := filepath.Join(root, "policy.json")
	put(t, policy, `{"bind_roots": ["`+filepath.Join(root, "opt")+`"]}`)
	put(t, filepath.Join(root, "opt/stacks/web/.argus-handed-back"), "web")
	sock := filepath.Join(t.TempDir(), "docker.sock")
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Skip("no unix sockets here")
	}
	old := filepath.Join(state, "stacks/web")
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `[{"Names":["/web-1"],"State":"running","Labels":{
		  "com.docker.compose.project":"web","com.docker.compose.project.working_dir":"`+old+`"}}]`)
	})}
	go func() { _ = srv.Serve(l) }()
	t.Cleanup(func() { _ = srv.Close() })
	f := &fakeCompose{}
	d := newDeployer(state, policy)
	d.HandedBack, d.compose, d.DockerSocket = filepath.Join(root, "opt/stacks"), f.run, sock
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})
	if r := d.Results(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})[0]; r.Result != "ok" {
		t.Fatalf("got %+v", r)
	}
}
