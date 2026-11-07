package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

// ArgusUpdate is Argus asking the companion on its own machine to update it:
// pull its image again and recreate its Compose project. Argus cannot do this
// itself without the Docker socket.
type ArgusUpdate struct {
	ID      string `json:"id"`
	Image   string `json:"image"`
	Project string `json:"project"`
}

type ArgusUpdateResult struct {
	ID      string `json:"id"`
	State   string `json:"state"` // running, ok, failed, rolled_back
	Summary string `json:"summary"`
	Log     string `json:"log"`
}

var (
	updateID    = regexp.MustCompile(`^[0-9a-f]{8,40}$`)
	imageRef    = regexp.MustCompile(`^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+(:[A-Za-z0-9._-]+)?$`)
	projectName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
)

const argusWait = 180 // seconds for the new Argus to report healthy

type ArgusUpdater struct {
	StateDir string
	docker   func(dir string, env []string, args ...string) (string, error)
	mu       sync.Mutex
	result   *ArgusUpdateResult
	done     map[string]bool
	busy     bool
}

func newArgusUpdater(stateDir string) *ArgusUpdater {
	return &ArgusUpdater{StateDir: stateDir, done: map[string]bool{},
		docker: func(dir string, env []string, args ...string) (string, error) {
			return runIn(dir, env, 15*time.Minute, "docker", args...)
		}}
}

func (a *ArgusUpdater) Busy() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.busy
}

// Take hands the newest result to the next report; a finished one only once.
func (a *ArgusUpdater) Take() *ArgusUpdateResult {
	a.mu.Lock()
	defer a.mu.Unlock()
	r := a.result
	if r != nil && r.State != "running" {
		a.result = nil
	}
	return r
}

// Start checks the request against what runs here and updates in the
// background. Only a project already running exactly that image is touched.
func (a *ArgusUpdater) Start(u ArgusUpdate, containers []Container) {
	a.mu.Lock()
	if a.busy || a.done[u.ID] {
		a.mu.Unlock()
		return
	}
	a.done[u.ID] = true
	fail := func(msg string) {
		a.result = &ArgusUpdateResult{ID: u.ID, State: "failed", Summary: msg}
		a.mu.Unlock()
	}
	if !updateID.MatchString(u.ID) || !imageRef.MatchString(u.Image) || !projectName.MatchString(u.Project) {
		fail("refused: not a usable update request")
		return
	}
	var mine []Container
	for _, c := range containers {
		if c.Project == u.Project {
			mine = append(mine, c)
		}
	}
	if len(mine) == 0 || mine[0].WorkingDir == "" {
		fail("refused: no Compose project " + u.Project + " runs here")
		return
	}
	if !anyImage(mine, u.Image) {
		fail("refused: project " + u.Project + " does not run " + u.Image)
		return
	}
	a.busy = true
	a.result = &ArgusUpdateResult{ID: u.ID, State: "running", Summary: "updating"}
	a.mu.Unlock()
	go func() {
		r := a.update(u, mine[0])
		a.mu.Lock()
		a.result, a.busy = r, false
		a.mu.Unlock()
	}()
}

func anyImage(cs []Container, image string) bool {
	for _, c := range cs {
		if c.Image == image {
			return true
		}
	}
	return false
}

func (a *ArgusUpdater) update(u ArgusUpdate, c Container) *ArgusUpdateResult {
	res := &ArgusUpdateResult{ID: u.ID}
	cfg := filepath.Join(a.StateDir, "argus-update-docker")
	_ = os.MkdirAll(cfg, 0o700) // a Docker config of its own: the root filesystem is read-only
	env := []string{"DOCKER_CONFIG=" + cfg}
	base := []string{"compose", "--project-directory", c.WorkingDir, "-p", u.Project}
	for _, f := range c.ConfigFiles {
		base = append(base, "-f", f)
	}
	run := func(args ...string) (string, error) {
		out, err := a.docker(c.WorkingDir, env, args...)
		res.Log += "$ docker " + strings.Join(args, " ") + "\n" + out
		return out, err
	}
	old, err := run("image", "inspect", "-f", "{{.Id}}", u.Image)
	old = strings.TrimSpace(old)
	if err != nil || old == "" {
		res.State, res.Summary = "failed", "could not read the image Argus runs now"
		return res
	}
	if _, err := run(append(base[:len(base):len(base)], "pull", "--quiet")...); err != nil {
		res.State, res.Summary = "failed", "pulling the new image failed; Argus kept running as it was"
		return res
	}
	now, _ := run("image", "inspect", "-f", "{{.Id}}", u.Image)
	if strings.TrimSpace(now) == old {
		res.State, res.Summary = "ok", "already up to date"
		return res
	}
	up := append(base[:len(base):len(base)], "up", "-d", "--wait", "--wait-timeout", fmt.Sprint(argusWait))
	if _, err := run(up...); err == nil {
		res.State, res.Summary = "ok", "updated; Argus is up and healthy"
		return res
	}
	// Not healthy: the old image back under the same tag, and the old Argus again.
	_, terr := run("tag", old, u.Image)
	_, uerr := run(up...)
	if terr != nil || uerr != nil {
		res.State, res.Summary = "failed", "the new Argus was not healthy, and putting the old one back failed too; see the log"
		return res
	}
	res.State, res.Summary = "rolled_back", "the new Argus was not healthy within 3 minutes; the old one is back"
	return res
}
