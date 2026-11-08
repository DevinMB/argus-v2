package main

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// Which packages an update would change, and for security updates why, from
// the distro's own sources only (Devin: no outside services):
//   - apt: the dry run names each package, its installed and new version and
//     its suite; the new version's changelog entry (apt-get changelog, from the
//     distro's own changelog server) names the CVEs and the DSA, DLA or USN.
//   - dnf: dnf updateinfo, already on the host, names each advisory
//     (RLSA-2026:1234) and its severity.
//   - apk: names and versions only.

// Package is one pending update.
type Package struct {
	Name       string   `json:"name"`
	From       string   `json:"from,omitempty"`
	To         string   `json:"to"`
	Security   bool     `json:"security,omitempty"`
	Source     string   `json:"source,omitempty"` // the source package, when it differs
	Advisories []string `json:"advisories,omitempty"`
	CVEs       []string `json:"cves,omitempty"`
	Severity   string   `json:"severity,omitempty"`
}

const maxPackages = 300

// Inst NAME [OLD] (NEW SUITE [ARCH]) ; OLD is missing for a new package.
var aptInst = regexp.MustCompile(`^Inst (\S+) (?:\[([^\]]*)\] )?\((\S+) ([^\[)]*?)\s*(?:\[[^\]]*\])?\)`)

func parseAptPackages(out string) []Package {
	var pkgs []Package
	for _, line := range strings.Split(out, "\n") {
		m := aptInst.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		pkgs = append(pkgs, Package{Name: m[1], From: m[2], To: m[3],
			Security: strings.Contains(m[4], "-security")})
	}
	return sortPackages(pkgs)
}

// name.arch  version  repo, before any "Obsoleting Packages" section.
func parseDnfPackages(out string) []Package {
	var pkgs []Package
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(line, "Obsoleting") {
			break
		}
		f := strings.Fields(line)
		if len(f) != 3 || !strings.Contains(f[0], ".") {
			continue
		}
		pkgs = append(pkgs, Package{Name: f[0][:strings.LastIndex(f[0], ".")], To: f[1]})
	}
	return pkgs
}

// dnf updateinfo list --security: "RLSA-2026:1234 Important/Sec. openssl-libs-1:3.2.2-6.el9_5.x86_64".
type advisory struct{ id, severity string }

func parseDnfAdvisories(out string) map[string][]advisory {
	by := map[string][]advisory{}
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) != 3 {
			continue
		}
		sev := strings.TrimSuffix(strings.SplitN(f[1], "/", 2)[0], ".")
		if sev == "Sec" || sev == "None" {
			sev = ""
		}
		by[nevraName(f[2])] = append(by[nevraName(f[2])], advisory{f[0], sev})
	}
	return by
}

// nevraName: "openssl-libs-1:3.2.2-6.el9_5.x86_64" -> "openssl-libs".
func nevraName(nevra string) string {
	s := nevra
	if i := strings.LastIndex(s, "."); i > 0 {
		s = s[:i] // the arch
	}
	for n := 0; n < 2; n++ { // release, then version
		if i := strings.LastIndex(s, "-"); i > 0 {
			s = s[:i]
		}
	}
	return s
}

func withAdvisories(pkgs []Package, by map[string][]advisory) []Package {
	rank := map[string]int{"Critical": 4, "Important": 3, "Moderate": 2, "Low": 1}
	for i := range pkgs {
		for _, a := range by[pkgs[i].Name] {
			pkgs[i].Security = true
			if !contains(pkgs[i].Advisories, a.id) {
				pkgs[i].Advisories = append(pkgs[i].Advisories, a.id)
			}
			if rank[a.severity] > rank[pkgs[i].Severity] {
				pkgs[i].Severity = a.severity
			}
		}
	}
	return sortPackages(pkgs)
}

// apk list -u: "busybox-1.36.1-r20 x86_64 {busybox} (GPL-2.0-only) [upgradable from: busybox-1.36.1-r19]".
var apkLine = regexp.MustCompile(`^(\S+) \S+ \{[^}]*\} \([^)]*\) \[upgradable from: (\S+)\]`)

func parseApkPackages(out string) []Package {
	var pkgs []Package
	for _, line := range strings.Split(out, "\n") {
		m := apkLine.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		name, to := splitApk(m[1])
		_, from := splitApk(m[2])
		pkgs = append(pkgs, Package{Name: name, From: from, To: to})
	}
	return sortPackages(pkgs)
}

// splitApk: "busybox-1.36.1-r20" -> ("busybox", "1.36.1-r20").
func splitApk(s string) (string, string) {
	i := strings.LastIndex(s, "-r")
	if i <= 0 {
		return s, ""
	}
	j := strings.LastIndex(s[:i], "-")
	if j <= 0 {
		return s, ""
	}
	return s[:j], s[j+1:]
}

// Security first, then by name; at most maxPackages.
func sortPackages(pkgs []Package) []Package {
	sort.SliceStable(pkgs, func(i, j int) bool {
		if pkgs[i].Security != pkgs[j].Security {
			return pkgs[i].Security
		}
		return pkgs[i].Name < pkgs[j].Name
	})
	if len(pkgs) > maxPackages {
		pkgs = pkgs[:maxPackages]
	}
	return pkgs
}

var (
	cveRe      = regexp.MustCompile(`CVE-\d{4}-\d{4,7}`)
	advisoryRe = regexp.MustCompile(`\b(?:DSA|DLA|USN)-\d+(?:-\d+)?\b`)
)

// changelogWhy reads the newest entry of a Debian changelog (up to its " -- "
// trailer): the CVEs and advisories it names.
func changelogWhy(text string) (cves, advisories []string) {
	var entry []string
	for i, line := range strings.Split(text, "\n") {
		if i > 0 && strings.HasPrefix(line, " -- ") {
			break
		}
		entry = append(entry, line)
	}
	body := strings.Join(entry, "\n")
	return uniq(cveRe.FindAllString(body, -1)), uniq(advisoryRe.FindAllString(body, -1))
}

// Changelogs is a cache of what each source package's new version fixes. A
// changelog is downloaded once per version, in the background (a report never
// waits for the network), a few per hourly look.
type Changelogs struct {
	mu    sync.Mutex
	known map[string][2][]string // "source new-version" -> cves, advisories
	busy  bool
	fetch func(pkg, version string) (string, error)
}

const changelogsPerLook = 15

func newChangelogs() *Changelogs {
	return &Changelogs{known: map[string][2][]string{}, fetch: func(pkg, version string) (string, error) {
		out, code, err := runCommandFor(20*time.Second, "apt-get", "changelog", pkg+"="+version)
		if err == nil && code != 0 {
			err = fmt.Errorf("apt-get changelog exited %d", code)
		}
		return out, err
	}}
}

// key and source name of a security package (its source package groups binaries).
func (cl *Changelogs) key(c *Collector, p *Package) string {
	if p.Source == "" {
		if out, code, err := c.run("dpkg-query", "-W", "-f=${source:Package}", p.Name); err == nil && code == 0 {
			if s := strings.TrimSpace(out); s != "" && s != p.Name {
				p.Source = s
			}
		}
	}
	src := p.Source
	if src == "" {
		src = p.Name
	}
	return src + " " + p.To
}

// fill puts what is known on the packages, and returns the ones still unknown.
func (cl *Changelogs) fill(c *Collector, pkgs []Package) []Package {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	var todo []Package
	for i := range pkgs {
		p := &pkgs[i]
		if !p.Security {
			continue
		}
		if why, ok := cl.known[cl.key(c, p)]; ok {
			p.CVEs, p.Advisories = why[0], why[1]
		} else {
			todo = append(todo, *p)
		}
	}
	return todo
}

// learn downloads the changelogs of todo (one per source and version), then
// calls done so the newest count picks them up.
func (cl *Changelogs) learn(c *Collector, todo []Package, done func()) {
	cl.mu.Lock()
	if cl.busy || len(todo) == 0 {
		cl.mu.Unlock()
		return
	}
	cl.busy = true
	cl.mu.Unlock()
	go func() {
		seen := map[string]bool{}
		n := 0
		for i := range todo {
			k := cl.key(c, &todo[i])
			if seen[k] || n >= changelogsPerLook {
				continue
			}
			seen[k] = true
			n++
			if out, err := cl.fetch(todo[i].Name, todo[i].To); err == nil {
				cves, adv := changelogWhy(out)
				cl.mu.Lock()
				cl.known[k] = [2][]string{cves, adv}
				cl.mu.Unlock()
			}
		}
		cl.mu.Lock()
		cl.busy = false
		cl.mu.Unlock()
		done()
	}()
}

func uniq(xs []string) []string {
	var out []string
	for _, x := range xs {
		if !contains(out, x) {
			out = append(out, x)
		}
	}
	return out
}

// rpmInstalled fills in each package's installed version, in one rpm call.
func (c *Collector) rpmInstalled(pkgs []Package) []Package {
	if len(pkgs) == 0 {
		return pkgs
	}
	args := []string{"-q", "--qf", "%{NAME} %{VERSION}-%{RELEASE}\\n"}
	for _, p := range pkgs {
		args = append(args, p.Name)
	}
	out, _, err := c.run("rpm", args...)
	if err != nil {
		return pkgs
	}
	have := map[string]string{}
	for _, line := range strings.Split(out, "\n") {
		if f := strings.Fields(line); len(f) == 2 {
			have[f[0]] = f[1]
		}
	}
	for i := range pkgs {
		pkgs[i].From = have[pkgs[i].Name]
	}
	return pkgs
}
