package main

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func fakeDocker(newID string, upFails int) (func(string, []string, ...string) (string, error), *[]string) {
	var calls []string
	inspects := 0
	return func(_ string, _ []string, args ...string) (string, error) {
		calls = append(calls, strings.Join(args, " "))
		switch {
		case args[0] == "image":
			inspects++
			if inspects == 1 {
				return "sha256:old\n", nil
			}
			return newID + "\n", nil
		case strings.Contains(strings.Join(args, " "), " up -d"):
			if upFails > 0 {
				upFails--
				return "argus-1 is unhealthy", errors.New("exit status 1")
			}
		}
		return "", nil
	}, &calls
}

func argusHere() []Container {
	return []Container{{Name: "argus-argus-1", Image: "ghcr.io/devinmb/argus:2", Project: "argus",
		WorkingDir: "/opt/argus", ConfigFiles: []string{"/opt/argus/compose.yml"}}}
}

func updateOnce(t *testing.T, a *ArgusUpdater, u ArgusUpdate) *ArgusUpdateResult {
	t.Helper()
	a.Start(u, argusHere())
	for a.Busy() {
		time.Sleep(time.Millisecond)
	}
	return a.Take()
}

func TestArgusUpdates(t *testing.T) {
	u := ArgusUpdate{ID: "0123456789abcdef", Image: "ghcr.io/devinmb/argus:2", Project: "argus"}
	a := newArgusUpdater(t.TempDir())
	var calls *[]string
	a.docker, calls = fakeDocker("sha256:new", 0)
	if r := updateOnce(t, a, u); r.State != "ok" || !strings.Contains(r.Summary, "healthy") {
		t.Fatalf("%+v", r)
	}
	joined := strings.Join(*calls, "\n")
	if !strings.Contains(joined, "compose --project-directory /opt/argus -p argus -f /opt/argus/compose.yml pull") {
		t.Errorf("calls:\n%s", joined)
	}
	if a.Start(u, argusHere()); a.Busy() || a.Take() != nil {
		t.Error("the same request ran twice")
	}

	// Not healthy in time: the old image goes back.
	b := newArgusUpdater(t.TempDir())
	b.docker, calls = fakeDocker("sha256:new", 1)
	if r := updateOnce(t, b, u); r.State != "rolled_back" {
		t.Fatalf("%+v", r)
	}
	if !strings.Contains(strings.Join(*calls, "\n"), "tag sha256:old ghcr.io/devinmb/argus:2") {
		t.Errorf("no rollback tag:\n%s", strings.Join(*calls, "\n"))
	}

	// Nothing newer: no restart.
	c := newArgusUpdater(t.TempDir())
	c.docker, calls = fakeDocker("sha256:old", 0)
	if r := updateOnce(t, c, u); r.State != "ok" || r.Summary != "already up to date" {
		t.Fatalf("%+v", r)
	}
	for _, call := range *calls {
		if strings.Contains(call, " up ") {
			t.Error("restarted Argus with nothing new")
		}
	}

	// Only a project that runs that very image.
	d := newArgusUpdater(t.TempDir())
	d.docker, _ = fakeDocker("sha256:new", 0)
	other := u
	other.ID, other.Image = "fedcba9876543210", "ghcr.io/evil/thing:1"
	if r := updateOnce(t, d, other); r.State != "failed" || !strings.HasPrefix(r.Summary, "refused") {
		t.Fatalf("%+v", r)
	}
}
