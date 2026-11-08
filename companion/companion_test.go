package main

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func pinOf(t *testing.T, srv *httptest.Server) string {
	t.Helper()
	sum := sha256.Sum256(srv.Certificate().RawSubjectPublicKeyInfo)
	return base64.StdEncoding.EncodeToString(sum[:])
}

// fakeArgus enrolls code "good-code" and signs replies like Argus does.
func fakeArgus(t *testing.T) (*httptest.Server, ed25519.PrivateKey) {
	pub, priv, _ := ed25519.GenerateKey(nil)
	mux := http.NewServeMux()
	mux.HandleFunc("POST /companion/enroll", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body["code"] != "good-code" {
			w.WriteHeader(http.StatusForbidden)
			_, _ = io.WriteString(w, `{"detail":"this code is unknown, used or expired"}`)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "c1", "name": "nas",
			"token": "argc_t", "public_key": base64.StdEncoding.EncodeToString(pub)})
	})
	mux.HandleFunc("POST /companion/report", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer argc_t" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		var rep Report
		_ = json.NewDecoder(r.Body).Decode(&rep)
		body := []byte(`{"name":"nas","report_every":60,"read_only":false,"checks":[` +
			`{"name":"web","kind":"tcp","target":"127.0.0.1:1","interval":30,"timeout":2}]}`)
		msg := append([]byte(domain+"c1\n"+rep.Nonce+"\n"), body...)
		w.Header().Set("X-Argus-Signature", base64.StdEncoding.EncodeToString(ed25519.Sign(priv, msg)))
		_, _ = w.Write(body)
	})
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	return srv, priv
}

func TestEnrollPinsArgusAndReportsVerify(t *testing.T) {
	srv, _ := fakeArgus(t)
	pin := pinOf(t, srv)
	if _, err := enroll(srv.URL, "AAAA"+pin[4:], "good-code", false); err == nil ||
		!strings.Contains(err.Error(), "not the pinned one") {
		t.Fatalf("a wrong pin must refuse to talk, got %v", err)
	}
	if _, err := enroll(srv.URL, pin, "bad", false); err == nil ||
		!strings.Contains(err.Error(), "HTTP 403: this code is unknown") {
		t.Fatalf("want Argus's reason, got %v", err)
	}
	c, err := enroll(srv.URL, pin, "good-code", true)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "etc", "companion.json")
	if err := c.save(path); err != nil {
		t.Fatal(err)
	}
	if st, _ := os.Stat(path); st.Mode().Perm() != 0o600 {
		t.Fatalf("config mode %v, want 0600", st.Mode().Perm())
	}
	c, _ = loadConfig(path)
	if !c.ReadOnly || c.Name != "nas" {
		t.Fatalf("config %+v", c)
	}
	reply, err := sendReport(pinnedClient(c.Pin), c, &Report{})
	if err != nil {
		t.Fatal(err)
	}
	if len(reply.Checks) != 1 || reply.Checks[0].Kind != "tcp" {
		t.Fatalf("reply %+v", reply)
	}
	// A reply signed by anyone else is ignored.
	other, _, _ := ed25519.GenerateKey(nil)
	c.PublicKey = base64.StdEncoding.EncodeToString(other)
	if _, err := sendReport(pinnedClient(c.Pin), c, &Report{}); err == nil ||
		!strings.Contains(err.Error(), "does not verify") {
		t.Fatalf("want a signature failure, got %v", err)
	}
	c.Token = "argc_revoked"
	var he *HTTPError
	if _, err := sendReport(pinnedClient(c.Pin), c, &Report{}); err == nil ||
		!asHTTP(err, &he) || he.Status != 401 {
		t.Fatalf("want 401, got %v", err)
	}
}

func asHTTP(err error, he **HTTPError) bool {
	e, ok := err.(*HTTPError)
	*he = e
	return ok
}

func TestSignatureCoversTheNonce(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(nil)
	key := base64.StdEncoding.EncodeToString(pub)
	body := []byte(`{"checks":[]}`)
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(priv, append([]byte(domain+"c1\nn1\n"), body...)))
	if err := verifyReply(key, "c1", "n1", body, sig); err != nil {
		t.Fatal(err)
	}
	for _, tc := range [][2]string{{"c1", "n2"}, {"c2", "n1"}} {
		if verifyReply(key, tc[0], tc[1], body, sig) == nil {
			t.Fatalf("a replay to %v must fail", tc)
		}
	}
	if verifyReply(key, "c1", "n1", []byte(`{"checks":[{}]}`), sig) == nil {
		t.Fatal("a changed body must fail")
	}
}

func TestParseMountInfo(t *testing.T) {
	text := `22 1 252:1 / / rw,relatime shared:1 - ext4 /dev/mapper/pve-root rw
23 22 0:21 / /proc rw - proc proc rw
24 22 252:1 /var/lib/x /srv/bind rw - ext4 /dev/mapper/pve-root rw
25 22 8:1 / /boot/efi rw - vfat /dev/sda1 rw
26 22 0:45 / /var/lib/docker/overlay2/abc/merged rw - overlay overlay rw
27 22 8:2 / /mnt/my\040disk rw - xfs /dev/sdb1 rw
28 22 0:50 / /rpool rw - zfs rpool rw
29 22 0:50 / /rpool/data rw - zfs rpool/data rw
30 22 0:60 / /mnt/unas rw - nfs4 unas:/backup rw
`
	got := parseMountInfo(text)
	want := []string{"/", "/mnt/my disk", "/rpool", "/rpool/data"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestParseContainers(t *testing.T) {
	body := []byte(`[{"Names":["/web"],"Image":"ghcr.io/x/web:abc","State":"running",
	  "Status":"Up 3 hours (healthy)","Labels":{"com.docker.compose.project":"web"},
	  "Ports":[{"IP":"0.0.0.0","PrivatePort":80,"PublicPort":8080,"Type":"tcp"},{"IP":"::","PrivatePort":80,"PublicPort":8080,"Type":"tcp"},
	           {"PrivatePort":53,"PublicPort":53,"Type":"udp"},{"PrivatePort":9000,"Type":"tcp"},
	           {"IP":"127.0.0.1","PrivatePort":5984,"PublicPort":5984,"Type":"tcp"}]},
	 {"Names":["/job"],"Image":"busybox","State":"exited","Status":"Exited (1) 2 days ago","Labels":{}}]`)
	got, err := parseContainers(body)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got[0].Ports, []int{5984, 8080}) {
		t.Fatalf("ports %v: want the published TCP ports once each", got[0].Ports)
	}
	if !reflect.DeepEqual(got[0].LocalPorts, []int{5984}) {
		t.Fatalf("local ports %v: want the one published on loopback only", got[0].LocalPorts)
	}
	if got[0].Name != "web" || got[0].Health != "healthy" || got[0].Project != "web" ||
		got[1].State != "exited" || got[1].Health != "" {
		t.Fatalf("got %+v", got)
	}
}

func TestDockerOverItsSocket(t *testing.T) {
	sock := filepath.Join(t.TempDir(), "docker.sock")
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Skip("no unix sockets here:", err)
	}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/containers/json" {
			w.WriteHeader(http.StatusTeapot) // the companion only ever reads
			return
		}
		_, _ = io.WriteString(w, `[{"Names":["/db"],"State":"running","Status":"Up 1 minute (health: starting)"}]`)
	})}
	go func() { _ = srv.Serve(l) }()
	t.Cleanup(func() { _ = srv.Close() })
	got, err := dockerContainers(sock)
	if err != nil || len(got) != 1 || got[0].Health != "starting" {
		t.Fatalf("got %+v, %v", got, err)
	}
}

func TestPackageManagerOutput(t *testing.T) {
	apt := `NOTE: This is only a simulation!
Inst libc6 [2.41-11] (2.41-12 Debian-Security:13/stable-security [amd64])
Inst curl [8.14.1-2] (8.14.1-3 Debian:13.2/stable [amd64])
Conf libc6 (2.41-12 Debian-Security:13/stable-security [amd64])
`
	if n, sec := parseAptSim(apt); n != 2 || sec != 1 {
		t.Fatalf("apt: %d, %d", n, sec)
	}
	dnf := `
kernel.x86_64                5.14.0-570.el9          baseos
openssl-libs.x86_64          1:3.2.2-6.el9_5.1       baseos
Obsoleting Packages
grub2-tools.x86_64           1:2.06-94.el9           baseos
`
	if n := parseDnfCheckUpdate(dnf); n != 2 {
		t.Fatalf("dnf: %d", n)
	}
	if got := parseFailedUnits("nginx.service loaded failed failed A web server\nx.timer loaded failed failed X\n"); !reflect.DeepEqual(got, []string{"nginx.service", "x.timer"}) {
		t.Fatalf("units: %v", got)
	}
}

func TestCollectWithFakeCommands(t *testing.T) {
	dir := t.TempDir()
	osr := filepath.Join(dir, "os-release")
	_ = os.WriteFile(osr, []byte("NAME=Debian\nPRETTY_NAME=\"Debian GNU/Linux 13 (trixie)\"\n"), 0o644)
	_ = os.WriteFile(filepath.Join(dir, "reboot"), nil, 0o644)
	c := newCollector()
	c.OSRelease, c.RebootFile, c.DockerSocket = osr, filepath.Join(dir, "reboot"), filepath.Join(dir, "none")
	c.run = func(string, ...string) (string, int, error) { return "", 0, nil }
	r := c.Collect(true)
	if r.OS != "Debian GNU/Linux 13 (trixie)" || !r.ReadOnly || r.Containers != nil ||
		r.RebootRequired == nil || !*r.RebootRequired {
		t.Fatalf("report %+v", r)
	}
	b, _ := json.Marshal(r)
	if !strings.Contains(string(b), `"containers":null`) {
		t.Fatalf("no Docker must be null, not empty: %s", b)
	}
}

func TestChecks(t *testing.T) {
	ok := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer ok.Close()
	ctx := context.Background()
	r := runCheck(ctx, CheckSpec{Name: "w", Kind: "http", Target: ok.URL, Timeout: 2})
	if r.OK || r.Error != "HTTP 502, expected 200" {
		t.Fatalf("http: %+v", r)
	}
	r = runCheck(ctx, CheckSpec{Name: "w", Kind: "http", Target: ok.URL, Timeout: 2, ExpectStatus: []int{502}})
	if !r.OK {
		t.Fatalf("http 502 expected: %+v", r)
	}
	addr := ok.Listener.Addr().String()
	if r := runCheck(ctx, CheckSpec{Kind: "tcp", Target: addr, Timeout: 2}); !r.OK {
		t.Fatalf("tcp: %+v", r)
	}
	if r := runCheck(ctx, CheckSpec{Kind: "tcp", Target: "127.0.0.1:1", Timeout: 2}); r.OK {
		t.Fatal("tcp to a closed port must fail")
	}
}

func TestCheckerQueuesResultsForTheReport(t *testing.T) {
	k := newChecker()
	k.Set([]CheckSpec{{Name: "a", Kind: "tcp", Target: "127.0.0.1:1", Interval: 30, Timeout: 1}})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go k.Loop(ctx)
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if got := k.Take(); len(got) == 1 {
			if got[0].Name != "a" || got[0].OK {
				t.Fatalf("got %+v", got)
			}
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal("no result within 5 s")
}

func TestValidationErrorsAreReadable(t *testing.T) {
	var d any
	_ = json.Unmarshal([]byte(`[{"loc":["body","version"],"msg":"String should have at most 100 characters"}]`), &d)
	if got := detailText(d); got != `["body","version"]: String should have at most 100 characters` {
		t.Fatal(got)
	}
}

func TestTheRunningKernelAgainstTheNewestInstalled(t *testing.T) {
	running := firstLine("/proc/sys/kernel/osrelease")
	c := newCollector()
	c.run = func(name string, args ...string) (string, int, error) {
		return "kernel-core-" + running + "    Sun 08 Nov 2026 05:52:01 AM EST\nkernel-core-5.14.0-1.el9    Sat\n", 0, nil
	}
	if r := c.newerKernelInstalled(); r == nil || *r {
		t.Fatalf("running the newest kernel is no reboot: %v", r)
	}
	c.run = func(name string, args ...string) (string, int, error) {
		return "kernel-core-9.9.9-1.el9.x86_64    Sun 08 Nov 2026\nkernel-core-" + running + "    Sat\n", 0, nil
	}
	if r := c.newerKernelInstalled(); r == nil || !*r {
		t.Fatalf("a newer kernel installed needs a reboot: %v", r)
	}
}
