package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Report is the host's state, sent once a minute. Fields the host cannot tell
// (no Docker, no systemd) are left out rather than reported as empty.
type Report struct {
	Nonce          string             `json:"nonce"`
	Version        string             `json:"version"`
	Hostname       string             `json:"hostname"`
	OS             string             `json:"os"`
	Kernel         string             `json:"kernel"`
	Arch           string             `json:"arch"`
	Uptime         float64            `json:"uptime"`
	ReadOnly       bool               `json:"read_only"`
	Containers     []Container        `json:"containers"` // null: no Docker here
	DockerError    string             `json:"docker_error,omitempty"`
	FailedUnits    []string           `json:"failed_units"` // null: no systemd
	Disks          []Disk             `json:"disks"`
	Updates        *Updates           `json:"updates,omitempty"`
	RebootRequired *bool              `json:"reboot_required,omitempty"`
	Checks         []CheckResult      `json:"checks"`
	Deploys        []DeployResult     `json:"deploys"`
	Local          LocalTime          `json:"local"`
	Snapshot       string             `json:"snapshot,omitempty"` // "zfs" when / can be snapshotted
	Patch          *PatchState        `json:"patch,omitempty"`
	PolicyRoots    []string           `json:"policy_roots,omitempty"` // folders deploys may use
	Collected      []Collected        `json:"collected,omitempty"`
	Proxmox        *Proxmox           `json:"proxmox,omitempty"` // on a Proxmox server
	GuestInstalls  []GuestResult      `json:"guest_installs,omitempty"`
	ArgusUpdate    *ArgusUpdateResult `json:"argus_update,omitempty"`
	Uninstall      *UninstallResult   `json:"uninstall,omitempty"`
	Watch          *WatchState        `json:"watch,omitempty"` // this host watches Argus
}

type Container struct {
	Name    string `json:"name"`
	Image   string `json:"image"`
	State   string `json:"state"`
	Health  string `json:"health"`
	Status  string `json:"status"`
	Project string `json:"project"`
	Ports   []int  `json:"ports"` // published TCP ports on the host
	// Of those, the ones published on the loopback address only (127.0.0.1:5984:5984):
	// nothing off the host reaches them, so a check for one runs on the host.
	LocalPorts []int `json:"local_ports,omitempty"`
	// Where Compose started it from: the project's folder and its compose files.
	WorkingDir  string   `json:"working_dir,omitempty"`
	ConfigFiles []string `json:"config_files,omitempty"`
}

type Disk struct {
	Mount string `json:"mount"`
	Total uint64 `json:"total"`
	Used  uint64 `json:"used"`
}

type Updates struct {
	Pending   *int      `json:"pending"`
	Security  *int      `json:"security"`
	Packages  []Package `json:"packages,omitempty"`
	Manager   string    `json:"manager"`
	CheckedAt float64   `json:"checked_at"`
	Error     string    `json:"error,omitempty"`
}

// Collector gathers a report. Paths are fields so tests can point them at fixtures.
type Collector struct {
	DockerSocket string
	MountInfo    string
	OSRelease    string
	RebootFile   string
	Proxmox      proxmoxPaths
	run          func(name string, args ...string) (string, int, error)
	// dnf check-update with a fresh cache, outside the read-only sandbox.
	dnfFresh func() (string, int, error)

	updates     *Updates
	updatesNext time.Time
	pveMu       sync.Mutex
	updMu       sync.Mutex // updates and updatesNext: a patch run asks for a recount
	changelogs  *Changelogs
	pve         *Proxmox
	pveNext     time.Time
	pveSince    time.Time // when the read in flight started; zero when none is
}

const updatesEvery = time.Hour

func newCollector() *Collector {
	return &Collector{
		DockerSocket: "/var/run/docker.sock",
		MountInfo:    "/proc/self/mountinfo",
		OSRelease:    "/etc/os-release",
		RebootFile:   "/var/run/reboot-required",
		Proxmox:      defaultProxmoxPaths,
		run:          runCommand,
		dnfFresh:     dnfFresh,
		changelogs:   newChangelogs(),
	}
}

// runCommand returns stdout and the exit code; err only when it could not run.
func runCommand(name string, args ...string) (string, int, error) {
	return runCommandFor(2*time.Minute, name, args...)
}

func runCommandFor(timeout time.Duration, name string, args ...string) (string, int, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = append(os.Environ(), "LC_ALL=C")
	out, err := cmd.Output()
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return string(out), exit.ExitCode(), nil
	}
	if err != nil {
		return "", -1, err
	}
	return string(out), 0, nil
}

func (c *Collector) Collect(readOnly bool) *Report {
	host, _ := os.Hostname()
	r := &Report{Version: version, Hostname: host, Arch: runtime.GOARCH, ReadOnly: readOnly,
		OS: osName(c.OSRelease), Kernel: firstLine("/proc/sys/kernel/osrelease"),
		Disks: []Disk{}, Checks: []CheckResult{}, Deploys: []DeployResult{}}
	if up := firstLine("/proc/uptime"); up != "" {
		r.Uptime, _ = strconv.ParseFloat(strings.Fields(up)[0], 64)
	}
	if _, err := os.Stat(c.DockerSocket); err == nil {
		r.Containers, err = dockerContainers(c.DockerSocket)
		if err != nil {
			r.Containers, r.DockerError = nil, err.Error()
		}
	}
	if _, err := exec.LookPath("systemctl"); err == nil {
		out, code, err := c.run("systemctl", "list-units", "--state=failed", "--plain",
			"--no-legend", "--no-pager")
		if err == nil && code == 0 {
			r.FailedUnits = parseFailedUnits(out)
		}
	}
	if b, err := os.ReadFile(c.MountInfo); err == nil {
		for _, m := range parseMountInfo(string(b)) {
			var st syscall.Statfs_t
			if syscall.Statfs(m, &st) != nil || st.Blocks == 0 {
				continue
			}
			bs := uint64(st.Bsize)
			used := (st.Blocks - st.Bfree) * bs
			// As df counts it: what is used of what a user could use.
			r.Disks = append(r.Disks, Disk{Mount: m, Total: used + st.Bavail*bs, Used: used})
		}
	}
	c.updMu.Lock()
	if time.Now().After(c.updatesNext) {
		c.updates = c.pendingUpdates()
		c.updatesNext = time.Now().Add(updatesEvery)
	}
	r.Updates = c.updates
	c.updMu.Unlock()
	r.RebootRequired = c.rebootRequired()
	r.Local = localNow(time.Now())
	if isProxmox(c.Proxmox) {
		r.Proxmox = c.proxmox(strings.SplitN(host, ".", 2)[0])
	}
	if zfsRoot(c.MountInfo) != "" && has("zfs") {
		r.Snapshot = "zfs"
	}
	return r
}

func firstLine(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(strings.SplitN(string(b), "\n", 2)[0])
}

func osName(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return runtime.GOOS
	}
	for _, line := range strings.Split(string(b), "\n") {
		if v, ok := strings.CutPrefix(line, "PRETTY_NAME="); ok {
			return strings.Trim(v, `"'`)
		}
	}
	return runtime.GOOS
}

// --- Docker, read-only: one GET on its socket ---

func dockerContainers(socket string) ([]Container, error) {
	client := &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
	}}
	resp, err := client.Get("http://docker/containers/json?all=1")
	if err != nil {
		return nil, fmt.Errorf("docker: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, fmt.Errorf("docker: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("docker said HTTP %d", resp.StatusCode)
	}
	return parseContainers(body)
}

func parseContainers(body []byte) ([]Container, error) {
	var raw []struct {
		Names  []string
		Image  string
		State  string
		Status string
		Labels map[string]string
		Ports  []struct {
			IP         string
			PublicPort int
			Type       string
		}
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, fmt.Errorf("docker: %w", err)
	}
	out := make([]Container, 0, len(raw))
	for _, c := range raw {
		name := ""
		if len(c.Names) > 0 {
			name = strings.TrimPrefix(c.Names[0], "/")
		}
		ports, open := []int{}, map[int]bool{}
		for _, p := range c.Ports {
			if p.PublicPort <= 0 || p.Type != "tcp" {
				continue
			}
			if !slices.Contains(ports, p.PublicPort) {
				ports = append(ports, p.PublicPort) // IPv4 and IPv6 list each port twice
			}
			if p.IP != "127.0.0.1" && p.IP != "::1" {
				open[p.PublicPort] = true
			}
		}
		slices.Sort(ports)
		var local []int
		for _, p := range ports {
			if !open[p] {
				local = append(local, p)
			}
		}
		out = append(out, Container{Name: name, Image: c.Image, State: c.State,
			Health: healthOf(c.Status), Status: c.Status,
			Project: c.Labels["com.docker.compose.project"], Ports: ports, LocalPorts: local,
			WorkingDir:  c.Labels["com.docker.compose.project.working_dir"],
			ConfigFiles: splitNonEmpty(c.Labels["com.docker.compose.project.config_files"], ",")})
	}
	return out, nil
}

func splitNonEmpty(s, sep string) []string {
	var out []string
	for _, p := range strings.Split(s, sep) {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func healthOf(status string) string {
	switch {
	case strings.Contains(status, "(unhealthy)"):
		return "unhealthy"
	case strings.Contains(status, "(healthy)"):
		return "healthy"
	case strings.Contains(status, "(health: starting)"):
		return "starting"
	}
	return ""
}

// --- systemd ---

func parseFailedUnits(out string) []string {
	units := []string{}
	for _, line := range strings.Split(out, "\n") {
		if f := strings.Fields(line); len(f) > 0 {
			units = append(units, f[0])
		}
	}
	return units
}

// --- disks ---

// Filesystems that hold data. Not tmpfs, overlay, squashfs, network mounts
// (statfs on a dead NFS server hangs) or the EFI partition.
var diskTypes = map[string]bool{"ext2": true, "ext3": true, "ext4": true, "xfs": true,
	"btrfs": true, "zfs": true, "f2fs": true, "bcachefs": true}

// parseMountInfo returns one mount point per device, the shortest, so bind
// mounts and btrfs subvolumes of one disk count once.
func parseMountInfo(text string) []string {
	best := map[string]string{}
	var order []string
	for _, line := range strings.Split(text, "\n") {
		pre, post, ok := strings.Cut(line, " - ")
		f, g := strings.Fields(pre), strings.Fields(post)
		if !ok || len(f) < 5 || len(g) < 1 || !diskTypes[g[0]] {
			continue
		}
		dev, mount := f[2], unescape(f[4])
		if strings.HasPrefix(mount, "/var/lib/docker/") || strings.HasPrefix(mount, "/snap/") {
			continue
		}
		if g[0] == "zfs" && len(g) > 1 {
			dev = g[1] // every dataset of a pool shares 0:NN; tell them apart by name
		}
		if old, seen := best[dev]; !seen {
			order = append(order, dev)
			best[dev] = mount
		} else if len(mount) < len(old) {
			best[dev] = mount
		}
	}
	out := make([]string, 0, len(order))
	for _, d := range order {
		out = append(out, best[d])
	}
	return out
}

func unescape(s string) string {
	r := strings.NewReplacer(`\040`, " ", `\011`, "\t", `\012`, "\n", `\134`, `\`)
	return r.Replace(s)
}

// --- package updates, at most hourly: never refreshes package lists itself ---

// SetUpdates keeps a fresh count (after a patch run), so the host does not show
// the updates it just installed until the next hourly look.
func (c *Collector) SetUpdates(u *Updates) {
	c.updMu.Lock()
	defer c.updMu.Unlock()
	c.updates, c.updatesNext = u, time.Now().Add(updatesEvery)
}

func (c *Collector) pendingUpdates() *Updates {
	u := &Updates{CheckedAt: float64(time.Now().Unix())}
	switch {
	case has("apt-get"):
		u.Manager = "apt"
		out, code, err := c.run("apt-get", "-s", "-o", "Debug::NoLocking=true",
			"-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache=", "dist-upgrade")
		if err != nil || code != 0 {
			u.Error = commandError("apt-get -s dist-upgrade", code, err)
			return u
		}
		n, sec := parseAptSim(out)
		u.Pending, u.Security = &n, &sec
		u.Packages = parseAptPackages(out)
		if c.changelogs != nil {
			todo := c.changelogs.fill(c, u.Packages)
			c.changelogs.learn(c, todo, func() {
				// Downloaded in the background: put what was learned on the newest count.
				c.updMu.Lock()
				if c.updates != nil {
					c.changelogs.fill(c, c.updates.Packages)
				}
				c.updMu.Unlock()
			})
		}
	case has("dnf"):
		u.Manager = "dnf"
		// The service's filesystem is read-only outside its own folder, and dnf
		// writes its log even for a cache-only read: point the log there.
		logdir := "--setopt=logdir=" + defaultState
		out, code, err := c.run("dnf", "-q", "-C", logdir, "check-update")
		if err == nil && code == 1 {
			// Cache-only fails when the cache is empty or expired (nothing keeps it
			// fresh on many hosts): refresh it outside the sandbox, as a patch run does.
			out, code, err = c.dnfFresh()
		}
		if err != nil || (code != 0 && code != 100) {
			u.Error = commandError("dnf check-update", code, err)
			if code > 0 || strings.TrimSpace(out) != "" {
				u.Error += ": " + lastWords(out) // what dnf (or systemd) said, not only that it failed
			}
			return u
		}
		n := parseDnfCheckUpdate(out)
		u.Pending = &n
		pkgs := c.rpmInstalled(parseDnfPackages(out))
		if out, code, err := c.run("dnf", "-q", "-C", logdir, "updateinfo", "list", "--security"); err == nil && code == 0 {
			sec := len(nonEmptyLines(out))
			u.Security = &sec
			pkgs = withAdvisories(pkgs, parseDnfAdvisories(out))
		}
		u.Packages = sortPackages(pkgs)
	case has("apk"):
		u.Manager = "apk"
		out, code, err := c.run("apk", "list", "-u")
		if err != nil || code != 0 {
			u.Error = commandError("apk list -u", code, err)
			return u
		}
		n := len(nonEmptyLines(out))
		u.Pending = &n
		u.Packages = parseApkPackages(out)
	default:
		return nil
	}
	return u
}

func has(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}

// dnfFresh runs dnf check-update with a cache refresh in a transient unit
// outside the service's read-only filesystem, and returns its output and exit code.
func dnfFresh() (string, int, error) {
	out, err := runOutside(5*time.Minute, []string{"LC_ALL=C"}, "sh", "-c", "dnf -q check-update 2>&1; echo argus-rc=$?")
	i := strings.LastIndex(out, "argus-rc=")
	if i < 0 {
		if err == nil {
			err = errors.New("no exit code from dnf")
		}
		return out, -1, err
	}
	code, cerr := strconv.Atoi(strings.TrimSpace(out[i+len("argus-rc="):]))
	if cerr != nil {
		return out, -1, cerr
	}
	return out[:i], code, nil
}

func lastWords(s string) string {
	lines := nonEmptyLines(s)
	if len(lines) == 0 {
		return "no output"
	}
	l := strings.TrimSpace(lines[len(lines)-1])
	if len(l) > 200 {
		l = l[:200]
	}
	return l
}

func commandError(what string, code int, err error) string {
	if err != nil {
		return what + ": " + err.Error()
	}
	return fmt.Sprintf("%s exited %d", what, code)
}

func nonEmptyLines(s string) []string {
	var out []string
	sc := bufio.NewScanner(strings.NewReader(s))
	for sc.Scan() {
		if t := strings.TrimSpace(sc.Text()); t != "" {
			out = append(out, t)
		}
	}
	return out
}

// parseAptSim counts "Inst" lines of apt-get -s, and those from a -security suite.
func parseAptSim(out string) (pending, security int) {
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(line, "Inst ") {
			pending++
			if strings.Contains(line, "-security") {
				security++
			}
		}
	}
	return
}

// parseDnfCheckUpdate counts package lines (name.arch version repo) before any
// "Obsoleting Packages" section.
func parseDnfCheckUpdate(out string) int {
	n := 0
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(line, "Obsoleting") {
			break
		}
		if f := strings.Fields(line); len(f) == 3 && strings.Contains(f[0], ".") {
			n++
		}
	}
	return n
}

func (c *Collector) rebootRequired() *bool {
	yes, no := true, false
	if _, err := os.Stat(c.RebootFile); err == nil {
		return &yes
	}
	if has("needs-restarting") {
		// It exits 1 both when a reboot is needed and when it fails (inside this
		// read-only sandbox dnf cannot write its cache), so read what it says
		// (slayerz-lab said "needs a reboot" right after rebooting).
		out, code, err := c.run("needs-restarting", "-r")
		low := strings.ToLower(out)
		switch {
		case err == nil && strings.Contains(low, "reboot is required"):
			return &yes
		case err == nil && (code == 0 || strings.Contains(low, "should not be necessary")):
			return &no
		}
		// No verdict: the running kernel against the newest one installed.
		return c.newerKernelInstalled()
	}
	if has("apt-get") {
		return &no // Debian and Ubuntu write the file when they need one
	}
	return nil
}

// newerKernelInstalled: whether a kernel newer than the running one is installed
// (rpm-based hosts), or nil when that cannot be told.
func (c *Collector) newerKernelInstalled() *bool {
	running := firstLine("/proc/sys/kernel/osrelease")
	out, code, err := c.run("rpm", "-q", "--last", "kernel-core")
	if err != nil || code != 0 || running == "" {
		return nil
	}
	lines := nonEmptyLines(out)
	if len(lines) == 0 {
		return nil
	}
	newest := strings.TrimPrefix(strings.Fields(lines[0])[0], "kernel-core-")
	v := newest != running
	return &v
}

// proxmox reads guests and backups in the background: a backup storage on a
// hung NFS mount must not stop the reports. Until a read finishes, the last
// one is sent, and after a while it says the storage is not answering.
func (c *Collector) proxmox(node string) *Proxmox {
	c.pveMu.Lock()
	defer c.pveMu.Unlock()
	now := time.Now()
	if c.pveSince.IsZero() && now.After(c.pveNext) {
		c.pveSince, c.pveNext = now, now.Add(proxmoxEvery)
		go func() {
			got := collectProxmox(c.Proxmox, node)
			c.pveMu.Lock()
			c.pve, c.pveSince = got, time.Time{}
			c.pveMu.Unlock()
		}()
	}
	if c.pve == nil || c.pveSince.IsZero() || now.Sub(c.pveSince) < 10*time.Minute {
		return c.pve
	}
	stale := *c.pve
	stale.Error = "the backup storage has not answered for " +
		strconv.Itoa(int(now.Sub(c.pveSince).Minutes())) + " minutes"
	return &stale
}
