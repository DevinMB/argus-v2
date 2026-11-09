package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Config is what enrollment leaves on the host, readable by root only.
type Config struct {
	URL       string `json:"url"`
	Pin       string `json:"pin,omitempty"` // SHA-256 of Argus's TLS public key, base64
	ID        string `json:"id"`
	Name      string `json:"name"`
	Token     string `json:"token"`
	PublicKey string `json:"public_key"` // Argus's ed25519 key; every reply must verify
	ReadOnly  bool   `json:"read_only"`
}

func loadConfig(path string) (*Config, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &c, nil
}

func (c *Config) save(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	b, _ := json.MarshalIndent(c, "", "  ")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// pinnedClient talks only to the Argus whose TLS key matches pin. With no pin
// (Argus behind a proxy with a real certificate) it verifies as usual.
func pinnedClient(pin string) *http.Client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.Proxy = nil // a proxy could not pass the pin anyway; keep the path direct
	if pin != "" {
		t.TLSClientConfig = &tls.Config{
			MinVersion: tls.VersionTLS12,
			// The CA check is replaced, not dropped: VerifyConnection demands the pinned key.
			InsecureSkipVerify: true, //nolint:gosec
			VerifyConnection: func(cs tls.ConnectionState) error {
				if len(cs.PeerCertificates) == 0 {
					return errors.New("no certificate from Argus")
				}
				return checkPin(cs.PeerCertificates[0], pin)
			},
		}
	}
	return &http.Client{Transport: t, Timeout: 30 * time.Second}
}

func checkPin(cert *x509.Certificate, pin string) error {
	sum := sha256.Sum256(cert.RawSubjectPublicKeyInfo)
	got := base64.StdEncoding.EncodeToString(sum[:])
	if subtle.ConstantTimeCompare([]byte(got), []byte(pin)) != 1 {
		return fmt.Errorf("Argus's TLS key is not the pinned one (got %s): refusing to talk", got)
	}
	return nil
}

// HTTPError is a non-2xx answer from Argus, with its reason when it gave one.
type HTTPError struct {
	Status int
	Detail string
}

func (e *HTTPError) Error() string {
	if e.Detail != "" {
		return fmt.Sprintf("Argus said HTTP %d: %s", e.Status, e.Detail)
	}
	return fmt.Sprintf("Argus said HTTP %d", e.Status)
}

func post(client *http.Client, url, token string, body any) ([]byte, http.Header, error) {
	b, err := json.Marshal(body)
	if err != nil {
		return nil, nil, err
	}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(b))
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "argus-companion/"+version)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode/100 != 2 {
		var e struct {
			Detail any `json:"detail"`
		}
		_ = json.Unmarshal(out, &e)
		detail := detailText(e.Detail)
		return nil, nil, &HTTPError{resp.StatusCode, detail}
	}
	return out, resp.Header, nil
}

// detailText reads FastAPI's detail: a string, or a list of validation errors.
func detailText(d any) string {
	switch v := d.(type) {
	case string:
		return v
	case []any:
		var parts []string
		for _, item := range v {
			m, _ := item.(map[string]any)
			loc, _ := json.Marshal(m["loc"])
			parts = append(parts, fmt.Sprintf("%s: %v", loc, m["msg"]))
		}
		return strings.Join(parts, "; ")
	}
	return ""
}

// enroll trades a single-use code for this host's token and Argus's signing key.
func enroll(url, pin, code string, readOnly bool) (*Config, error) {
	host, _ := os.Hostname()
	url = strings.TrimRight(url, "/")
	out, _, err := post(pinnedClient(pin), url+"/companion/enroll", "",
		map[string]string{"code": code, "hostname": host})
	if err != nil {
		return nil, err
	}
	var r struct {
		ID, Name, Token string
		PublicKey       string `json:"public_key"`
	}
	if err := json.Unmarshal(out, &r); err != nil {
		return nil, err
	}
	if k, err := base64.StdEncoding.DecodeString(r.PublicKey); err != nil || len(k) != ed25519.PublicKeySize {
		return nil, errors.New("Argus sent no valid signing key")
	}
	return &Config{URL: url, Pin: pin, ID: r.ID, Name: r.Name, Token: r.Token,
		PublicKey: r.PublicKey, ReadOnly: readOnly}, nil
}

// Reply is what Argus answers to a report: the checks to run from here.
type Reply struct {
	Name        string      `json:"name"`
	ReportEvery int         `json:"report_every"`
	ReadOnly    bool        `json:"read_only"`
	Checks      []CheckSpec `json:"checks"`
	Deploy      *DeployPlan `json:"deploy"`
	Patch       *PatchGrant `json:"patch"`
	Collect     []string    `json:"collect"` // Compose projects to read for adoption
	Companion   *Build      `json:"companion"`
	// On a Proxmox server: guests to add the companion to.
	GuestInstalls []GuestInstall `json:"guest_installs"`
	// On the machine Argus runs on: update Argus (Settings, Update Argus).
	ArgusUpdate *ArgusUpdate `json:"argus_update"`
	// Uninstall from this host (Uninstall on its page, confirmed with the password).
	Uninstall bool `json:"uninstall"`
	// To a host that watches Argus (decision 19).
	Watch *WatchSaid `json:"watch"`
	// The numeric ID of the bot Argus alerts with (public, never the token), so
	// "argus-companion watch" can refuse it.
	ArgusBot string `json:"argus_bot"`
}

// Build is the companion Argus serves for this host's CPU.
type Build struct {
	SHA256 string `json:"sha256"`
}

// Domain separates these signatures from anything else the key might sign.
const domain = "argus-companion-reply-v1\n"

func verifyReply(publicKey, id, nonce string, body []byte, sig string) error {
	key, err := base64.StdEncoding.DecodeString(publicKey)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return errors.New("no valid pinned signing key in the config")
	}
	s, err := base64.StdEncoding.DecodeString(sig)
	if err != nil {
		return errors.New("reply has no valid signature")
	}
	msg := []byte(domain + id + "\n" + nonce + "\n")
	if !ed25519.Verify(ed25519.PublicKey(key), append(msg, body...), s) {
		return errors.New("reply signature does not verify: ignoring it")
	}
	return nil
}

func newNonce() string {
	b := make([]byte, 18)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

// sendReport posts the host's state and returns Argus's verified reply.
func sendReport(client *http.Client, c *Config, r *Report) (*Reply, error) {
	r.Nonce = newNonce()
	out, hdr, err := post(client, c.URL+"/companion/report", c.Token, r)
	if err != nil {
		return nil, err
	}
	if err := verifyReply(c.PublicKey, c.ID, r.Nonce, out, hdr.Get("X-Argus-Signature")); err != nil {
		return nil, err
	}
	var reply Reply
	if err := json.Unmarshal(out, &reply); err != nil {
		return nil, err
	}
	return &reply, nil
}
