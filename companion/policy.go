package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// Policy is what this host will never run, whatever Argus or the deploy repo
// says. It lives on the host (/etc/argus-companion/policy.json) and changing it
// needs someone on the host: a compromised Argus cannot loosen it.
type Policy struct {
	AllowPrivileged     bool     `json:"allow_privileged"`
	AllowHostNamespaces bool     `json:"allow_host_namespaces"` // network, pid, ipc, userns: host
	AllowDockerSocket   bool     `json:"allow_docker_socket"`
	AllowDevices        bool     `json:"allow_devices"`
	AllowedCaps         []string `json:"allowed_caps"`
	// Host folders a bind mount may come from, besides the stack's own folder.
	BindRoots []string `json:"bind_roots"`
	// Host files any stack may mount read-only.
	ReadOnlyBinds []string `json:"read_only_binds"`
	// True: this program is updated only by re-running the install command, never
	// by itself when Argus serves a newer build.
	ManualUpdates bool `json:"manual_updates"`
	// True: on a Proxmox server, never add the companion to a guest when Argus
	// asks (Add Argus on a guest row).
	NoGuestInstalls bool `json:"no_guest_installs"`
	// True: never update Argus when it asks (Settings, Update Argus).
	NoArgusUpdates bool `json:"no_argus_updates"`
}

func defaultPolicy() *Policy {
	return &Policy{
		AllowedCaps:   []string{"CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "NET_BIND_SERVICE", "NET_RAW", "SETGID", "SETUID"},
		BindRoots:     []string{"/srv", "/opt", "/mnt", "/media"},
		ReadOnlyBinds: []string{"/etc/localtime", "/etc/timezone", "/etc/ssl/certs"},
	}
}

// loadPolicy reads the policy file; a missing file means the defaults. A file
// that does not parse is an error, and the deployer then refuses everything.
func loadPolicy(path string) (*Policy, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return defaultPolicy(), nil
	}
	if err != nil {
		return nil, err
	}
	p := &Policy{}
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(p); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return p, nil
}

type composeConfig struct {
	Services map[string]struct {
		Privileged  bool     `json:"privileged"`
		NetworkMode string   `json:"network_mode"`
		Pid         string   `json:"pid"`
		Ipc         string   `json:"ipc"`
		UsernsMode  string   `json:"userns_mode"`
		CapAdd      []string `json:"cap_add"`
		Devices     []any    `json:"devices"`
		SecurityOpt []string `json:"security_opt"`
		Volumes     []struct {
			Type     string `json:"type"`
			Source   string `json:"source"`
			ReadOnly bool   `json:"read_only"`
		} `json:"volumes"`
	} `json:"services"`
}

// Check returns what the stack does that the policy forbids. config is
// `docker compose config --format json` run in stage; relative binds there
// point into stage and are judged as the stack folder `dir` they will become.
func (p *Policy) Check(config []byte, stage, dir string) []string {
	var c composeConfig
	if err := json.Unmarshal(config, &c); err != nil {
		return []string{"the compose file did not resolve: " + err.Error()}
	}
	var bad []string
	names := make([]string, 0, len(c.Services))
	for n := range c.Services {
		names = append(names, n)
	}
	slices.Sort(names)
	for _, n := range names {
		s := c.Services[n]
		no := func(why string) { bad = append(bad, n+": "+why) }
		if s.Privileged && !p.AllowPrivileged {
			no("privileged")
		}
		if !p.AllowHostNamespaces {
			for what, mode := range map[string]string{"network": s.NetworkMode, "pid": s.Pid, "ipc": s.Ipc, "userns": s.UsernsMode} {
				if mode == "host" {
					no(what + " of the host")
				}
			}
		}
		if len(s.Devices) > 0 && !p.AllowDevices {
			no("devices")
		}
		for _, cp := range s.CapAdd {
			cp = strings.TrimPrefix(strings.ToUpper(cp), "CAP_")
			if !slices.Contains(p.AllowedCaps, cp) {
				no("capability " + cp)
			}
		}
		for _, o := range s.SecurityOpt {
			if strings.Contains(o, "unconfined") {
				no("security_opt " + o)
			}
		}
		for _, v := range s.Volumes {
			if v.Type != "bind" {
				continue
			}
			src := filepath.Clean(v.Source)
			if src == stage || strings.HasPrefix(src, stage+"/") {
				continue // inside the stack's own folder
			}
			if strings.HasSuffix(src, "docker.sock") {
				if !p.AllowDockerSocket {
					no("the Docker socket")
				}
				continue
			}
			if v.ReadOnly && slices.Contains(p.ReadOnlyBinds, src) {
				continue
			}
			if !slices.ContainsFunc(p.BindRoots, func(root string) bool {
				root = filepath.Clean(root)
				return src == root || strings.HasPrefix(src, root+"/")
			}) {
				no("a bind from " + src)
			}
		}
	}
	_ = dir
	return bad
}
