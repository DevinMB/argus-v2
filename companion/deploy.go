package main

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"syscall"
	"time"
)

// DeployPlan is the deploy part of Argus's signed reply: what this host runs.
type DeployPlan struct {
	Commit string      `json:"commit"`
	Stacks []StackSpec `json:"stacks"`
	Pull   *PullCred   `json:"pull"`
}

type StackSpec struct {
	Path   string            `json:"path"`
	Name   string            `json:"name"`
	Digest string            `json:"digest"`
	Files  map[string]string `json:"files"`         // relative path -> base64
	Env    string            `json:"env"`           // the stack's .env, from Argus's sealed secrets
	Dir    string            `json:"dir,omitempty"` // adopted: deploy in its own folder
}

// PullCred is short-lived registry read access, used for one pull and never stored.
type PullCred struct {
	Registry string `json:"registry"`
	Username string `json:"username"`
	Password string `json:"password"`
}

type DeployResult struct {
	Path   string  `json:"path"`
	Digest string  `json:"digest"`
	Result string  `json:"result"` // ok, failed, refused, rolled_back
	Detail string  `json:"detail,omitempty"`
	At     float64 `json:"at"`
}

// retryFailedAfter: a digest that failed is tried again only after this long,
// so a broken commit is not redeployed every minute.
const retryFailedAfter = time.Hour

// Deployer applies stacks with Docker Compose, in StacksDir/<name>. It writes only
// the files the deploy repo has for the stack; anything else in the folder
// (./data from a relative bind) is the stack's own and stays.
type Deployer struct {
	StateDir     string
	DockerSocket string
	StacksDir    string
	HandedBack   string // where an uninstall hands stacks back: /opt/stacks
	PolicyPath   string
	Policy       *Policy
	PolicyErr    error
	policyHash   string // sha256 of the policy file as last read
	compose      func(dir string, env []string, args ...string) (string, error)
	now          func() time.Time
}

type deployState struct {
	Results map[string]DeployResult `json:"results"` // by stack path
	Managed map[string][]string     `json:"managed"` // stack name -> files we wrote
	// The policy a stack was refused under (its file's sha256): a changed policy
	// is worth another try, an unchanged one is not.
	RefusedUnder map[string]string `json:"refused_under,omitempty"`
	// Stacks deployed in the folder an earlier uninstall handed them back to:
	// stack name -> folder. Argus does not know these; the companion keeps them.
	Homes map[string]string `json:"homes,omitempty"`
}

func newDeployer(stateDir string, policyPath string) *Deployer {
	return &Deployer{StateDir: stateDir, StacksDir: filepath.Join(stateDir, "stacks"),
		HandedBack: "/opt/stacks", PolicyPath: policyPath, DockerSocket: "/var/run/docker.sock", compose: runCompose, now: time.Now}
}

func (d *Deployer) load() *deployState {
	s := &deployState{Results: map[string]DeployResult{}, Managed: map[string][]string{}}
	if b, err := os.ReadFile(filepath.Join(d.StateDir, "deploys.json")); err == nil {
		_ = json.Unmarshal(b, s)
	}
	if s.Results == nil {
		s.Results = map[string]DeployResult{}
	}
	if s.Managed == nil {
		s.Managed = map[string][]string{}
	}
	return s
}

func (d *Deployer) save(s *deployState) error {
	b, _ := json.MarshalIndent(s, "", "  ")
	tmp := filepath.Join(d.StateDir, "deploys.json.tmp")
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(d.StateDir, "deploys.json"))
}

// Results are what the next report carries: the newest attempt per wanted stack.
func (d *Deployer) Results(plan *DeployPlan) []DeployResult {
	out := []DeployResult{}
	s := d.load()
	if plan == nil {
		// Just started (a self-update restarts every companion at once): no plan
		// yet, so say what is on disk, or Argus shows every app as deploying again.
		for _, r := range s.Results {
			out = append(out, r)
		}
		sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
		return out
	}
	for _, st := range plan.Stacks {
		if r, ok := s.Results[st.Path]; ok {
			out = append(out, r)
		}
	}
	return out
}

// Due says whether a stack needs a deploy now.
func (d *Deployer) due(s *deployState, st StackSpec) bool {
	r, ok := s.Results[st.Path]
	if !ok || r.Digest != st.Digest {
		return true
	}
	if r.Result == "refused" {
		return s.RefusedUnder[st.Path] != d.policyHash // refused until the stack or the policy changes
	}
	if r.Result == "ok" {
		return false
	}
	return d.now().Sub(time.Unix(int64(r.At), 0)) > retryFailedAfter
}

// Apply deploys every stack whose digest changed, one at a time. It returns
// whether it did anything, so the caller can report at once.
func (d *Deployer) Apply(plan *DeployPlan) bool {
	if plan == nil {
		return false
	}
	if err := os.MkdirAll(d.StacksDir, 0o700); err != nil {
		return false
	}
	if d.PolicyPath != "" { // re-read each time: a change on the host applies at once
		d.Policy, d.PolicyErr = loadPolicy(d.PolicyPath)
		d.policyHash = ""
		if b, err := os.ReadFile(d.PolicyPath); err == nil {
			sum := sha256.Sum256(b)
			d.policyHash = hex.EncodeToString(sum[:])
		}
	}
	s := d.load()
	did := false
	for _, st := range plan.Stacks {
		if !d.due(s, st) {
			continue
		}
		did = true
		r := d.one(s, st, plan.Pull)
		r.At = float64(d.now().Unix())
		s.Results[st.Path] = r
		if s.RefusedUnder == nil {
			s.RefusedUnder = map[string]string{}
		}
		if r.Result == "refused" {
			s.RefusedUnder[st.Path] = d.policyHash
		} else {
			delete(s.RefusedUnder, st.Path)
		}
		_ = d.save(s)
	}
	return did
}

func (d *Deployer) one(s *deployState, st StackSpec, pull *PullCred) DeployResult {
	res := func(result, detail string) DeployResult {
		return DeployResult{Path: st.Path, Digest: st.Digest, Result: result, Detail: tail(detail, 1800)}
	}
	if d.PolicyErr != nil {
		return res("refused", "the host's policy file is unreadable, so nothing deploys: "+d.PolicyErr.Error())
	}
	if !validName(st.Name) {
		return res("refused", "not a usable stack name: "+st.Name)
	}
	files := map[string][]byte{}
	for name, b64 := range st.Files {
		if !safeRel(name) {
			return res("refused", "a file path leaves the stack folder: "+name)
		}
		b, err := base64.StdEncoding.DecodeString(b64)
		if err != nil {
			return res("failed", "a file did not decode: "+name)
		}
		files[name] = b
	}
	dir := filepath.Join(d.StacksDir, st.Name)
	home, found := d.home(s, st.Name)
	if st.Dir == "" && home != "" {
		dir = home
	}
	if st.Dir != "" {
		if err := d.inPlace(st.Dir); err != nil {
			return res("refused", err.Error())
		}
		dir = filepath.Clean(st.Dir)
	}
	// The same project running from another folder: deploying here would start a
	// second copy and leave data beside the old one. It must be linked in Argus.
	if cs, err := dockerContainers(d.DockerSocket); err == nil {
		for _, c := range cs {
			wd := filepath.Clean(c.WorkingDir)
			// Its own old folder is fine: an uninstall moved it to dir.
			mine := home != "" && wd == filepath.Join(d.StacksDir, st.Name)
			if c.Project == st.Name && c.WorkingDir != "" && wd != dir && !mine {
				return res("refused", fmt.Sprintf("%s already runs from %s; link it on the host's card in Argus, so it keeps that folder",
					st.Name, c.WorkingDir))
			}
		}
	}

	// 1. The policy, on the stack as Compose resolves it, before anything is written.
	stage, err := os.MkdirTemp(d.StateDir, ".stage-")
	if err != nil {
		return res("failed", err.Error())
	}
	defer os.RemoveAll(stage)
	if err := writeFiles(stage, files, st.Env); err != nil {
		return res("failed", err.Error())
	}
	out, err := d.compose(stage, nil, "-p", st.Name, "config", "--format", "json")
	if err != nil {
		return res("failed", "docker compose config: "+out)
	}
	if bad := d.Policy.Check([]byte(out), stage, dir); len(bad) > 0 {
		return res("refused", "this host's policy refuses it: "+strings.Join(bad, "; "))
	}

	// 2. Write the files, keeping the old ones to roll back to.
	backup := filepath.Join(d.StateDir, "backup", st.Name)
	_ = os.RemoveAll(backup)
	old := s.Managed[st.Name]
	if found {
		// The files the handed-back folder already runs: the ones to roll back to.
		for name := range files {
			if _, err := os.Stat(filepath.Join(dir, name)); err == nil {
				old = append(old, name)
			}
		}
		sort.Strings(old)
	}
	hadDeploy := len(old) > 0
	for _, name := range append(slices.Clone(old), ".env") {
		if b, err := os.ReadFile(filepath.Join(dir, name)); err == nil {
			_ = writeFile(filepath.Join(backup, name), b, 0o600)
		}
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return res("failed", err.Error())
	}
	for _, name := range old {
		if _, keep := files[name]; !keep {
			_ = os.Remove(filepath.Join(dir, name))
		}
	}
	if err := writeFiles(dir, files, st.Env); err != nil {
		if errors.Is(err, syscall.EROFS) {
			return res("failed", fmt.Sprintf("the companion's service may not write to %s: if its policy allows that folder now, run the Add a host command on this host again (without --code) to apply it", dir))
		}
		return res("failed", err.Error())
	}
	managed := make([]string, 0, len(files))
	for name := range files {
		managed = append(managed, name)
	}
	sort.Strings(managed)
	s.Managed[st.Name] = managed

	// 3. Pull with the one-time credential, then start and wait for healthy.
	if found {
		if s.Homes == nil {
			s.Homes = map[string]string{}
		}
		s.Homes[st.Name] = dir
	}
	err = d.upAndWait(dir, st.Name, pull, &out)
	if err == nil {
		if found {
			return res("ok", "deployed in "+dir+", where the uninstall left it")
		}
		return res("ok", "")
	}
	why := out
	if !hadDeploy {
		return res("failed", why)
	}
	// 4. Roll back to the files that ran before.
	for _, name := range managed {
		_ = os.Remove(filepath.Join(dir, name))
	}
	_ = filepath.Walk(backup, func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			rel, _ := filepath.Rel(backup, p)
			b, _ := os.ReadFile(p)
			mode := os.FileMode(0o644)
			if rel == ".env" {
				mode = 0o600
			}
			_ = writeFile(filepath.Join(dir, rel), b, mode)
		}
		return nil
	})
	s.Managed[st.Name] = old
	var back string
	if err := d.upAndWait(dir, st.Name, pull, &back); err != nil {
		return res("failed", why+"\nrolling back failed too: "+back)
	}
	return res("rolled_back", why)
}

// handedBackMark is the file an uninstall leaves in each stack folder it hands
// back, holding the stack's name (see handBackScript). It stays while Argus
// deploys there, so a second uninstall and reinstall finds the folder again.
const handedBackMark = ".argus-handed-back"

// home is the folder a stack Argus creates lives in when it is not the usual
// one: remembered from an earlier deploy, or, when this companion has never
// deployed it, a folder an uninstall handed it back to. found is true for the
// second, the first deploy there.
func (d *Deployer) home(s *deployState, name string) (dir string, found bool) {
	if h := s.Homes[name]; h != "" {
		return h, false
	}
	if d.HandedBack == "" || len(s.Managed[name]) > 0 {
		return "", false
	}
	if _, err := os.Stat(filepath.Join(d.StacksDir, name)); err == nil {
		return "", false
	}
	for _, cand := range []string{name, name + "-argus"} {
		dir := filepath.Join(d.HandedBack, cand)
		b, err := readPlain(filepath.Join(dir, handedBackMark))
		if err != nil || strings.TrimSpace(string(b)) != name || !d.allowed(dir) {
			continue
		}
		return dir, true
	}
	return "", false
}

// allowed: dir is inside a folder this host's policy lets stacks live in.
func (d *Deployer) allowed(dir string) bool {
	if d.Policy == nil {
		return false
	}
	return slices.ContainsFunc(d.Policy.BindRoots, func(root string) bool {
		root = filepath.Clean(root)
		return dir == root || strings.HasPrefix(dir, root+"/")
	})
}

func (d *Deployer) upAndWait(dir, name string, pull *PullCred, out *string) error {
	env := []string{}
	cfgDir, err := os.MkdirTemp(d.StateDir, ".docker-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(cfgDir) // the credential is gone with it
	if pull != nil && pull.Password != "" {
		auth := base64.StdEncoding.EncodeToString([]byte(pull.Username + ":" + pull.Password))
		cfg, _ := json.Marshal(map[string]any{"auths": map[string]any{pull.Registry: map[string]string{"auth": auth}}})
		if err := os.WriteFile(filepath.Join(cfgDir, "config.json"), cfg, 0o600); err != nil {
			return err
		}
	}
	env = append(env, "DOCKER_CONFIG="+cfgDir)
	if o, err := d.compose(dir, env, "-p", name, "pull", "--quiet"); err != nil {
		*out = "pull: " + o
		return err
	}
	if o, err := d.compose(dir, env, "-p", name, "up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "300"); err != nil {
		*out = "up: " + o
		return err
	}
	return nil
}

func runCompose(dir string, env []string, args ...string) (string, error) {
	full := append([]string{"compose", "--project-directory", dir}, args...)
	return runIn(dir, env, 15*time.Minute, "docker", full...)
}

func writeFiles(dir string, files map[string][]byte, env string) error {
	for name, b := range files {
		if err := writeFile(filepath.Join(dir, name), b, 0o644); err != nil {
			return err
		}
	}
	// The stack's secrets: readable by root only.
	return writeFile(filepath.Join(dir, ".env"), []byte(env), 0o600)
}

func writeFile(path string, b []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".argus-tmp"
	if err := os.WriteFile(tmp, b, mode); err != nil {
		return err
	}
	if err := os.Chmod(tmp, mode); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func safeRel(name string) bool {
	if name == "" || filepath.IsAbs(name) || name == ".env" {
		return false
	}
	clean := filepath.Clean(name)
	return clean == name && !strings.HasPrefix(clean, "..") && !strings.Contains(clean, "/../")
}

func validName(n string) bool {
	if n == "" || len(n) > 63 {
		return false
	}
	for _, r := range n {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-' || r == '_' || r == '.') {
			return false
		}
	}
	return n[0] != '.' && n[0] != '-'
}

func tail(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n:]
}

// runIn runs a command in dir with extra env and a timeout; output is stdout and stderr.
func runIn(dir string, env []string, timeout time.Duration, name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = dir
	cmd.Env = append(append(os.Environ(), "LC_ALL=C"), env...)
	out, err := cmd.CombinedOutput()
	if ctx.Err() != nil {
		return string(out), fmt.Errorf("timed out after %s", timeout)
	}
	return string(out), err
}
