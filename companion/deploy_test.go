package main

import (
	"encoding/base64"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

// fakeCompose answers like docker compose: `config` echoes a canned resolution,
// `up` fails while failUp is set. It records each call and the pull credential.
type fakeCompose struct {
	calls   []string
	config  string
	failUp  bool
	authSaw string
}

func (f *fakeCompose) run(dir string, env []string, args ...string) (string, error) {
	f.calls = append(f.calls, strings.Join(args[2:], " "))
	switch args[2] {
	case "config":
		if f.config != "" {
			return f.config, nil
		}
		return `{"services":{"web":{"image":"nginx","volumes":[{"type":"bind","source":"` + dir + `/data","target":"/d"}]}}}`, nil
	case "pull":
		for _, e := range env {
			if p, ok := strings.CutPrefix(e, "DOCKER_CONFIG="); ok {
				b, _ := os.ReadFile(filepath.Join(p, "config.json"))
				f.authSaw = string(b)
			}
		}
	case "up":
		if f.failUp {
			return "container web is unhealthy", os.ErrDeadlineExceeded
		}
	}
	return "", nil
}

func newTestDeployer(t *testing.T) (*Deployer, *fakeCompose) {
	f := &fakeCompose{}
	d := newDeployer(t.TempDir(), filepath.Join(t.TempDir(), "absent.json"))
	d.compose = f.run
	d.DockerSocket = filepath.Join(t.TempDir(), "no-docker")
	return d, f
}

func stack(digest, compose string) StackSpec {
	return StackSpec{Path: "sites/home/web", Name: "web", Digest: digest,
		Files: map[string]string{"compose.yml": b64(compose), "conf/site.conf": b64("listen 80;")},
		Env:   "TOKEN='s3cret'\n"}
}

func TestDeployWritesFilesKeepsDataAndRollsBack(t *testing.T) {
	d, f := newTestDeployer(t)
	pull := &PullCred{Registry: "ghcr.io", Username: "x-access-token", Password: "ghs_short"}
	if !d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}, Pull: pull}) {
		t.Fatal("a new stack must deploy")
	}
	dir := filepath.Join(d.StacksDir, "web")
	if b, _ := os.ReadFile(filepath.Join(dir, "compose.yml")); string(b) != "v1" {
		t.Fatalf("compose.yml = %q", b)
	}
	if st, _ := os.Stat(filepath.Join(dir, ".env")); st.Mode().Perm() != 0o600 {
		t.Fatalf(".env mode %v", st.Mode().Perm())
	}
	if !strings.Contains(f.authSaw, b64("x-access-token:ghs_short")) {
		t.Fatalf("the pull did not get the credential: %q", f.authSaw)
	}
	if entries, _ := filepath.Glob(filepath.Join(d.StateDir, ".docker-*")); len(entries) != 0 {
		t.Fatalf("the credential outlived the deploy: %v", entries)
	}
	r := d.Results(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})
	if len(r) != 1 || r[0].Result != "ok" {
		t.Fatalf("results %+v", r)
	}
	// The stack's own data, from a relative bind, is never touched.
	_ = os.WriteFile(filepath.Join(dir, "data"), []byte("precious"), 0o644)
	n := len(f.calls)
	if d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}}) || len(f.calls) != n {
		t.Fatal("an unchanged digest must not redeploy")
	}

	f.failUp = true
	// v2 is unhealthy only on its first try; the rollback's up succeeds.
	orig := f.run
	tries := 0
	d.compose = func(dir string, env []string, args ...string) (string, error) {
		if args[2] == "up" {
			tries++
			f.failUp = tries == 1
		}
		return orig(dir, env, args...)
	}
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d2", "v2")}})
	r = d.Results(&DeployPlan{Stacks: []StackSpec{stack("d2", "v2")}})
	if r[0].Result != "rolled_back" || !strings.Contains(r[0].Detail, "unhealthy") {
		t.Fatalf("want a rollback, got %+v", r[0])
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "compose.yml")); string(b) != "v1" {
		t.Fatalf("rollback left compose.yml = %q", b)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "data")); string(b) != "precious" {
		t.Fatal("the stack's data was touched")
	}
	// A failed digest waits an hour before another try.
	if d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d2", "v2")}}) {
		t.Fatal("retried a failed digest at once")
	}
	d.now = func() time.Time { return time.Now().Add(2 * time.Hour) }
	if !d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d2", "v2")}}) {
		t.Fatal("did not retry after an hour")
	}
}

func TestPolicyRefusesBeforeWriting(t *testing.T) {
	d, f := newTestDeployer(t)
	f.config = `{"services":{"bad":{"privileged":true,"network_mode":"host",
	  "cap_add":["SYS_ADMIN"],"volumes":[{"type":"bind","source":"/var/run/docker.sock"},
	  {"type":"bind","source":"/etc","read_only":true},{"type":"bind","source":"/etc/localtime","read_only":true},
	  {"type":"bind","source":"/srv/media"},{"type":"volume","source":"db"}]}}}`
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})
	r := d.Results(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})[0]
	for _, want := range []string{"privileged", "network of the host", "capability SYS_ADMIN",
		"the Docker socket", "a bind from /etc;"} {
		if !strings.Contains(r.Detail+";", want) {
			t.Errorf("refusal %q lacks %q", r.Detail, want)
		}
	}
	if r.Result != "refused" || strings.Contains(r.Detail, "localtime") || strings.Contains(r.Detail, "/srv") {
		t.Fatalf("got %+v", r)
	}
	if _, err := os.Stat(filepath.Join(d.StacksDir, "web", "compose.yml")); err == nil {
		t.Fatal("a refused stack was written")
	}
	for _, c := range f.calls {
		if strings.HasPrefix(c, "pull") || strings.HasPrefix(c, "up") {
			t.Fatal("a refused stack was started")
		}
	}
	// Refused stays refused while nothing changes...
	if d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}}) {
		t.Fatal("retried a refused stack with the same policy")
	}
	// ...and a changed policy on the host is worth another try at once.
	_ = os.WriteFile(d.PolicyPath, []byte(`{"bind_roots": ["/srv", "/home/devin"]}`), 0o644)
	if !d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}}) {
		t.Fatal("did not retry after the host's policy changed")
	}
}

func TestUnsafePathsAndABrokenPolicyRefuse(t *testing.T) {
	d, _ := newTestDeployer(t)
	s := stack("d1", "v1")
	s.Files["../escape"] = b64("x")
	d.Apply(&DeployPlan{Stacks: []StackSpec{s}})
	if r := d.Results(&DeployPlan{Stacks: []StackSpec{s}})[0]; r.Result != "refused" {
		t.Fatalf("got %+v", r)
	}
	d2, _ := newTestDeployer(t)
	_ = os.WriteFile(d2.PolicyPath, []byte(`{"allow_everything": true}`), 0o644)
	d2.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})
	if r := d2.Results(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})[0]; r.Result != "refused" ||
		!strings.Contains(r.Detail, "policy file is unreadable") {
		t.Fatalf("a policy that does not parse must refuse everything, got %+v", r)
	}
}

func TestAProjectRunningFromAnotherFolderIsNotStartedTwice(t *testing.T) {
	d, f := newTestDeployer(t)
	sock := filepath.Join(t.TempDir(), "docker.sock")
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Skip("no unix sockets here")
	}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `[{"Names":["/web-1"],"State":"running","Labels":{
		  "com.docker.compose.project":"web","com.docker.compose.project.working_dir":"/opt/homelab-stacks/sites/home/web"}}]`)
	})}
	go func() { _ = srv.Serve(l) }()
	t.Cleanup(func() { _ = srv.Close() })
	d.DockerSocket = sock
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})
	r := d.Results(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})[0]
	if r.Result != "refused" || !strings.Contains(r.Detail, "already runs from /opt/homelab-stacks/sites/home/web") {
		t.Fatalf("got %+v", r)
	}
	if len(f.calls) != 0 {
		t.Fatalf("nothing may run: %v", f.calls)
	}
}

// Restarted (every companion restarts on a self-update), a companion has no
// plan until Argus replies: its first report still says what it deployed.
func TestAfterARestartTheFirstReportStillSaysWhatIsDeployed(t *testing.T) {
	d, _ := newTestDeployer(t)
	plan := &DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}}
	d.Apply(plan)
	again := newDeployer(d.StateDir, d.PolicyPath) // the same folders, a fresh process
	got := again.Results(nil)
	if len(got) != 1 || got[0].Digest != "d1" || got[0].Result != "ok" {
		t.Fatalf("results after a restart: %+v", got)
	}
}
