package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type fakeTg struct {
	sent []string
	fail bool
}

func newTestWatcher(t *testing.T, watching bool) (*Watcher, *fakeTg, *time.Time) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "watch.json")
	if watching {
		put(t, path, `{"token":"123:secret","chat_id":"42","bot":"argus_watchdog_bot"}`)
	}
	now := time.Date(2026, 11, 9, 14, 2, 0, 0, time.Local)
	tg := &fakeTg{}
	w := newWatcher(&Config{URL: "https://192.168.10.18:8443", Name: "slayerz-lab"})
	w.path, w.lastSeen = path, now
	w.now = func() time.Time { return now }
	w.send = func(_ *WatchConfig, text string) error {
		if tg.fail {
			return os.ErrDeadlineExceeded
		}
		tg.sent = append(tg.sent, text)
		return nil
	}
	w.check = func(*WatchConfig) error { return nil }
	return w, tg, &now
}

func TestAWatcherSaysWhenArgusStopsAndComesBack(t *testing.T) {
	w, tg, now := newTestWatcher(t, true)
	*now = now.Add(4 * time.Minute)
	w.Tick()
	if len(tg.sent) != 0 {
		t.Fatalf("4 minutes is not down yet: %v", tg.sent)
	}
	*now = now.Add(2 * time.Minute)
	w.Tick()
	w.Tick()
	if len(tg.sent) != 1 || !strings.Contains(tg.sent[0], "Argus is not answering. slayerz-lab last heard from it at 14:02") ||
		!strings.Contains(tg.sent[0], "6 minutes ago") || !strings.Contains(tg.sent[0], "https://192.168.10.18:8443") {
		t.Fatalf("one down message: %v", tg.sent)
	}
	*now = now.Add(6 * time.Hour)
	w.Tick()
	if len(tg.sent) != 2 || !strings.HasPrefix(tg.sent[1], "Argus is still not answering, since 14:02") {
		t.Fatalf("a reminder after 6 hours: %v", tg.sent)
	}
	w.Heard(&WatchSaid{})
	if len(tg.sent) != 3 || !strings.Contains(tg.sent[2], "Argus is answering again. slayerz-lab could not reach it from 14:02 to 20:08") {
		t.Fatalf("back: %v", tg.sent)
	}
	w.Heard(&WatchSaid{})
	w.Tick()
	if len(tg.sent) != 3 {
		t.Fatalf("nothing more while up: %v", tg.sent)
	}
}

func TestAFailedSendIsTriedAgain(t *testing.T) {
	w, tg, now := newTestWatcher(t, true)
	tg.fail = true
	*now = now.Add(10 * time.Minute)
	w.Tick()
	tg.fail = false
	w.Tick()
	if len(tg.sent) != 1 {
		t.Fatalf("the down message on the next tick: %v", tg.sent)
	}
	tg.fail = true
	*now = now.Add(time.Minute)
	w.Heard(nil)
	tg.fail = false
	*now = now.Add(time.Minute)
	w.Heard(nil)
	if len(tg.sent) != 2 || !strings.Contains(tg.sent[1], "from 14:02 to 14:14") {
		t.Fatalf("the back message, from when it went quiet: %v", tg.sent)
	}
}

func TestAnUpdateIsNotAnOutage(t *testing.T) {
	w, tg, now := newTestWatcher(t, true)
	w.Heard(&WatchSaid{QuietUntil: float64(now.Add(15 * time.Minute).Unix())})
	*now = now.Add(10 * time.Minute)
	w.Tick()
	if len(tg.sent) != 0 {
		t.Fatalf("quiet while Argus updates: %v", tg.sent)
	}
	*now = now.Add(10 * time.Minute)
	w.Tick()
	if len(tg.sent) != 1 {
		t.Fatalf("still silent after the window is an outage: %v", tg.sent)
	}
}

func TestArgusFailingToAlertIsPassedOnOnce(t *testing.T) {
	w, tg, _ := newTestWatcher(t, true)
	why := "Telegram refused the message: HTTP 401 (check the bot token)"
	w.Heard(&WatchSaid{AlertsFailing: why})
	w.Heard(&WatchSaid{AlertsFailing: why})
	if len(tg.sent) != 1 || !strings.Contains(tg.sent[0], "Argus is up but its last alert did not go out: "+why) ||
		!strings.Contains(tg.sent[0], "https://192.168.10.18:8443/#/alerts") {
		t.Fatalf("relayed once: %v", tg.sent)
	}
	w.Heard(&WatchSaid{})
	if len(tg.sent) != 2 || tg.sent[1] != "Argus is sending its own alerts again." {
		t.Fatalf("and the fix: %v", tg.sent)
	}
}

func TestATestIsSentOncePerNumber(t *testing.T) {
	w, tg, _ := newTestWatcher(t, true)
	w.Heard(&WatchSaid{Test: 1})
	w.Heard(&WatchSaid{Test: 1})
	if len(tg.sent) != 1 || !strings.HasPrefix(tg.sent[0], "Test from slayerz-lab") {
		t.Fatalf("one test: %v", tg.sent)
	}
	s := w.State()
	if s.Test != 1 || s.Bot != "argus_watchdog_bot" || s.BotID != "123" || s.CheckedAt == 0 {
		t.Fatalf("state %+v", s)
	}
}

func TestAHostThatDoesNotWatchSaysNothing(t *testing.T) {
	w, tg, now := newTestWatcher(t, false)
	*now = now.Add(time.Hour)
	w.Tick()
	w.Heard(&WatchSaid{AlertsFailing: "x", Test: 3})
	if len(tg.sent) != 0 || w.State() != nil {
		t.Fatalf("sent %v, state %+v", tg.sent, w.State())
	}
}

// Telegram's errors never carry the token, even when the network fails.
func TestTelegramErrorsNeverShowTheToken(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(401)
		_, _ = w.Write([]byte(`{"ok":false,"description":"Unauthorized"}`))
	}))
	defer srv.Close()
	b := newBot("123:topsecret")
	b.api = srv.URL
	err := b.send("42", "hi")
	if err == nil || strings.Contains(err.Error(), "topsecret") || !strings.Contains(err.Error(), "check the bot token") {
		t.Fatalf("got %v", err)
	}
	b.api = "http://127.0.0.1:1"
	err = b.send("42", "hi")
	if err == nil || strings.Contains(err.Error(), "topsecret") || !strings.HasPrefix(err.Error(), "could not reach Telegram") {
		t.Fatalf("got %v", err)
	}
}

func TestRememberArgusBot(t *testing.T) {
	p := filepath.Join(t.TempDir(), "state", argusBotName)
	rememberArgusBot(p, "123456")
	if b, _ := os.ReadFile(p); string(b) != "123456" {
		t.Fatalf("saved %q", b)
	}
	if (&WatchConfig{Token: "123456:AAsecret"}).BotID() != "123456" {
		t.Fatal("a token's bot ID is the part before the colon")
	}
	rememberArgusBot(p, "")
	if _, err := os.Stat(p); !os.IsNotExist(err) {
		t.Fatal("no bot at Argus: the file goes")
	}
}
