package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Watching Argus (design decision 19). A stopped Argus cannot say it stopped,
// so a host its owner picks watches it: `sudo /usr/local/bin/argus-companion watch` stores a
// second Telegram bot's token on this host only (it never goes to Argus), and
// the companion messages through it when Argus has not answered for 5 minutes,
// when it answers again, and when Argus says its own alerts are not going out.

const watchFile = "/etc/argus-companion/watch.json"

// WatchConfig is the watchdog bot, kept root-only on this host.
type WatchConfig struct {
	Token  string `json:"token"`
	ChatID string `json:"chat_id"`
	Bot    string `json:"bot"` // its @username, for Argus's page
}

// BotID is the token's public part (before the colon): Argus compares it with
// its own bot's, so the watchdog is never the bot Argus sends with.
func (w *WatchConfig) BotID() string {
	id, _, _ := strings.Cut(w.Token, ":")
	return id
}

func loadWatch(path string) (*WatchConfig, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var w WatchConfig
	if err := json.Unmarshal(b, &w); err != nil || w.Token == "" || w.ChatID == "" {
		return nil, fmt.Errorf("%s is not a usable watch file", path)
	}
	return &w, nil
}

// ---- Telegram, with errors that never carry the token ----

type tgBot struct {
	api    string
	token  string
	client *http.Client
}

func newBot(token string) *tgBot {
	return &tgBot{api: "https://api.telegram.org", token: token, client: &http.Client{Timeout: 15 * time.Second}}
}

func (b *tgBot) call(method string, form url.Values, out any) error {
	resp, err := b.client.PostForm(b.api+"/bot"+b.token+"/"+method, form)
	if err != nil {
		// A *url.Error names the URL, and the URL holds the token: say only what kind.
		var ue *url.Error
		if errors.As(err, &ue) {
			err = ue.Err
		}
		why := "unreachable"
		if errors.Is(err, os.ErrDeadlineExceeded) || strings.Contains(err.Error(), "Timeout") {
			why = "timed out"
		}
		return fmt.Errorf("could not reach Telegram (%s)", why)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var r struct {
		OK          bool            `json:"ok"`
		Description string          `json:"description"`
		Result      json.RawMessage `json:"result"`
	}
	_ = json.Unmarshal(body, &r)
	if resp.StatusCode != 200 || !r.OK {
		hint := map[int]string{401: " (check the bot token)", 404: " (check the bot token)",
			400: " (check the chat)", 403: " (the bot cannot message you: send it a message first)"}[resp.StatusCode]
		d := strings.ReplaceAll(r.Description, b.token, "…")
		if len(d) > 200 {
			d = d[:200]
		}
		return fmt.Errorf("Telegram said HTTP %d: %s%s", resp.StatusCode, d, hint)
	}
	if out != nil {
		return json.Unmarshal(r.Result, out)
	}
	return nil
}

func (b *tgBot) username() (string, error) {
	var me struct {
		Username string `json:"username"`
	}
	if err := b.call("getMe", nil, &me); err != nil {
		return "", err
	}
	return me.Username, nil
}

func (b *tgBot) send(chat, text string) error {
	return b.call("sendMessage", url.Values{"chat_id": {chat}, "text": {text},
		"disable_web_page_preview": {"true"}}, nil)
}

// newestChat is the private chat that messaged the bot most recently, if any.
func (b *tgBot) newestChat() (id, name string, err error) {
	var ups []struct {
		Message *struct {
			Chat struct {
				ID        int64  `json:"id"`
				Type      string `json:"type"`
				FirstName string `json:"first_name"`
				Username  string `json:"username"`
			} `json:"chat"`
		} `json:"message"`
	}
	if err := b.call("getUpdates", url.Values{"allowed_updates": {`["message"]`}}, &ups); err != nil {
		return "", "", err
	}
	for i := len(ups) - 1; i >= 0; i-- {
		if m := ups[i].Message; m != nil && m.Chat.Type == "private" {
			name := m.Chat.FirstName
			if name == "" {
				name = m.Chat.Username
			}
			return fmt.Sprint(m.Chat.ID), name, nil
		}
	}
	return "", "", nil
}

// ---- the watcher, inside the running companion ----

// WatchSaid is what Argus's reply tells a watcher.
type WatchSaid struct {
	QuietUntil    float64 `json:"quiet_until,omitempty"`    // updating: silence is expected until then
	AlertsFailing string  `json:"alerts_failing,omitempty"` // why Argus's newest alert did not go out
	Test          int     `json:"test,omitempty"`           // send a test once per new number
}

// WatchState is what a watcher tells Argus, for the Alerts page.
type WatchState struct {
	Bot       string  `json:"bot"`
	BotID     string  `json:"bot_id"`
	CheckedAt float64 `json:"checked_at,omitempty"` // Telegram last answered
	Error     string  `json:"error,omitempty"`
	Test      int     `json:"test,omitempty"` // the newest test sent
	TestError string  `json:"test_error,omitempty"`
}

const (
	downAfter   = 5 * time.Minute
	remindEvery = 6 * time.Hour
	checkEvery  = time.Hour
)

type Watcher struct {
	mu        sync.Mutex
	path      string
	argus     string // Argus's address, for the messages
	host      string
	now       func() time.Time
	send      func(w *WatchConfig, text string) error
	check     func(w *WatchConfig) error
	lastSeen  time.Time
	quiet     time.Time
	downSent  time.Time // when the newest "not answering" went out; zero while up
	downFrom  time.Time // the last reply before it
	relayed   string    // the alerts-failing reason already passed on
	checkedAt time.Time
	state     WatchState
}

func newWatcher(c *Config) *Watcher {
	return &Watcher{
		path: watchFile, argus: c.URL, host: c.Name, now: time.Now, lastSeen: time.Now(),
		send:  func(w *WatchConfig, text string) error { return newBot(w.Token).send(w.ChatID, text) },
		check: func(w *WatchConfig) error { _, err := newBot(w.Token).username(); return err },
	}
}

func (wt *Watcher) config() *WatchConfig {
	w, err := loadWatch(wt.path)
	if err != nil {
		return nil
	}
	return w
}

func clock(t time.Time) string { return t.Local().Format("15:04") }

// clockZ adds the time zone; a sentence with two times names it once, last.
func clockZ(t time.Time) string { return t.Local().Format("15:04 MST") }

func span(d time.Duration) string {
	switch {
	case d < 2*time.Minute:
		return "a minute"
	case d < 2*time.Hour:
		return fmt.Sprintf("%d minutes", int(d.Minutes()))
	case d < 48*time.Hour:
		return fmt.Sprintf("%d hours", int(d.Hours()))
	}
	return fmt.Sprintf("%d days", int(d.Hours()/24))
}

// Heard is called with every reply Argus signed. A 401 counts too: Argus is up,
// it just no longer knows this host.
func (wt *Watcher) Heard(said *WatchSaid) {
	wt.mu.Lock()
	defer wt.mu.Unlock()
	now := wt.now()
	w := wt.config()
	if w != nil && !wt.downSent.IsZero() {
		msg := fmt.Sprintf("Argus is answering again. %s could not reach it from %s to %s (%s).",
			wt.host, clock(wt.downFrom), clockZ(now), span(now.Sub(wt.downFrom)))
		if wt.send(w, msg) == nil {
			wt.downSent = time.Time{}
		}
	} else if w == nil {
		wt.downSent = time.Time{}
	}
	wt.lastSeen = now
	if said == nil {
		said = &WatchSaid{}
	}
	wt.quiet = time.Unix(int64(said.QuietUntil), 0)
	if w == nil {
		return
	}
	if said.AlertsFailing != wt.relayed {
		var msg string
		if said.AlertsFailing != "" {
			msg = fmt.Sprintf("Argus is up but its last alert did not go out: %s. Fix it on Alerts: %s/#/alerts",
				said.AlertsFailing, strings.TrimRight(wt.argus, "/"))
		} else {
			msg = "Argus is sending its own alerts again."
		}
		if wt.send(w, msg) == nil {
			wt.relayed = said.AlertsFailing
		}
	}
	if said.Test > wt.state.Test {
		err := wt.send(w, fmt.Sprintf("Test from %s: this bot tells you if Argus (%s) stops answering. Nothing to do.",
			wt.host, wt.argus))
		wt.state.Test, wt.state.TestError = said.Test, ""
		if err != nil {
			wt.state.TestError = err.Error()
		} else {
			wt.checkedAt, wt.state.CheckedAt, wt.state.Error = now, float64(now.Unix()), ""
		}
	}
}

// Tick runs every 30 seconds, reply or not.
func (wt *Watcher) Tick() {
	wt.mu.Lock()
	defer wt.mu.Unlock()
	w := wt.config()
	if w == nil {
		return
	}
	now := wt.now()
	if now.Sub(wt.checkedAt) >= checkEvery {
		wt.checkedAt = now
		if err := wt.check(w); err != nil {
			wt.state.Error = err.Error()
		} else {
			wt.state.CheckedAt, wt.state.Error = float64(now.Unix()), ""
		}
	}
	silent := now.Sub(wt.lastSeen)
	if silent < downAfter || now.Before(wt.quiet) {
		return
	}
	var msg string
	switch {
	case wt.downSent.IsZero():
		msg = fmt.Sprintf("Argus is not answering. %s last heard from it at %s (%s ago). Check that %s is up.",
			wt.host, clockZ(wt.lastSeen), span(silent), wt.argus)
	case now.Sub(wt.downSent) >= remindEvery:
		msg = fmt.Sprintf("Argus is still not answering, since %s (%s). Check that %s is up.",
			clockZ(wt.lastSeen), span(silent), wt.argus)
	default:
		return
	}
	if err := wt.send(w, msg); err == nil {
		wt.downSent, wt.downFrom = now, wt.lastSeen
	}
}

// State is the report's watch part; nil when this host does not watch.
func (wt *Watcher) State() *WatchState {
	wt.mu.Lock()
	defer wt.mu.Unlock()
	w := wt.config()
	if w == nil {
		return nil
	}
	s := wt.state
	s.Bot, s.BotID = w.Bot, w.BotID()
	return &s
}

// ---- `argus-companion watch`, run by hand as root ----

// argusBotName is the state file holding the ID of the bot Argus alerts with.
const argusBotName = "argus-bot"

// rememberArgusBot keeps the latest ID Argus sent, writing only on a change.
func rememberArgusBot(path, id string) {
	if old, _ := os.ReadFile(path); string(old) == id {
		return
	}
	if id == "" {
		_ = os.Remove(path)
		return
	}
	_ = writeFile(path, []byte(id), 0o644)
}

func watchHere(config, path, argusBot string, args []string) int {
	if os.Geteuid() != 0 {
		fmt.Fprintln(os.Stderr, "run this as root: sudo /usr/local/bin/argus-companion watch")
		return 1
	}
	c, err := loadConfig(config)
	if err != nil {
		fmt.Fprintln(os.Stderr, "this host is not enrolled yet: add it to Argus first (Hosts, Add a host)")
		return 1
	}
	mode := ""
	if len(args) > 0 {
		mode = args[0]
	}
	switch mode {
	case "--off":
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		fmt.Printf("%s no longer watches Argus. Argus's Alerts page shows it within a minute.\n", c.Name)
		return 0
	case "--test":
		w, err := loadWatch(path)
		if err != nil {
			fmt.Fprintln(os.Stderr, "this host does not watch Argus yet: run sudo /usr/local/bin/argus-companion watch")
			return 1
		}
		if err := newBot(w.Token).send(w.ChatID, fmt.Sprintf("Test from %s: this bot tells you if Argus (%s) stops answering. Nothing to do.", c.Name, c.URL)); err != nil {
			fmt.Fprintln(os.Stderr, "the test did not go out:", err)
			return 1
		}
		fmt.Println("Sent. Check Telegram.")
		return 0
	case "":
	default:
		fmt.Fprintln(os.Stderr, "usage: sudo /usr/local/bin/argus-companion watch [--off|--test]")
		return 2
	}
	in := bufio.NewReader(os.Stdin)
	fmt.Printf("%s will message you if Argus (%s) stops answering.\n\n", c.Name, c.URL)
	fmt.Println("1. In Telegram, ask @BotFather for a new bot (/newbot). Use a second bot,")
	fmt.Println("   not the one Argus sends its alerts with.")
	fmt.Print("2. Paste its token here (it stays on this host): ")
	token := readSecret(in)
	if !strings.Contains(token, ":") {
		fmt.Fprintln(os.Stderr, "\nthat is not a bot token: it looks like 123456789:AA...")
		return 1
	}
	if own, _ := os.ReadFile(argusBot); len(own) > 0 && (&WatchConfig{Token: token}).BotID() == string(own) {
		fmt.Fprintln(os.Stderr, "\nthat is the bot Argus already sends its alerts with. If Telegram blocked it,")
		fmt.Fprintln(os.Stderr, "Argus's alerts and this watcher would go quiet together. Ask @BotFather for a")
		fmt.Fprintln(os.Stderr, "second bot (/newbot) and run this again with its token.")
		return 1
	}
	bot := newBot(token)
	name, err := bot.username()
	if err != nil {
		fmt.Fprintln(os.Stderr, "\n"+err.Error())
		return 1
	}
	fmt.Printf("\n3. Send any message to @%s from the account that should get these alerts.\n   Waiting", name)
	var chat, who string
	for i := 0; i < 60 && chat == ""; i++ {
		if chat, who, err = bot.newestChat(); err != nil {
			fmt.Fprintln(os.Stderr, "\n"+err.Error())
			return 1
		}
		if chat == "" {
			fmt.Print(".")
			time.Sleep(3 * time.Second)
		}
	}
	if chat == "" {
		fmt.Fprintf(os.Stderr, "\nno message reached @%s in 3 minutes; run this again when ready\n", name)
		return 1
	}
	w := &WatchConfig{Token: token, ChatID: chat, Bot: name}
	if err := bot.send(chat, fmt.Sprintf("%s now watches Argus (%s). This bot speaks only if Argus stops answering or cannot send its own alerts.", c.Name, c.URL)); err != nil {
		fmt.Fprintln(os.Stderr, "\nthe first message did not go out:", err)
		return 1
	}
	b, _ := json.MarshalIndent(w, "", "  ")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err == nil {
		err = writeFile(path, b, 0o600)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "\nsaving it:", err)
		return 1
	}
	fmt.Printf("\n\nDone: %s watches Argus and told %s so on Telegram.\n", c.Name, who)
	fmt.Println("Argus's Alerts page lists it within a minute. Stop with: sudo /usr/local/bin/argus-companion watch --off")
	return 0
}

// readSecret reads a line without echoing it, when stdin is a terminal.
func readSecret(in *bufio.Reader) string {
	if exec.Command("stty", "-F", "/dev/tty", "-echo").Run() == nil {
		defer func() { _ = exec.Command("stty", "-F", "/dev/tty", "echo").Run() }()
	}
	line, _ := in.ReadString('\n')
	return strings.TrimSpace(line)
}
