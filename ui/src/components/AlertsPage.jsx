import { useCallback, useEffect, useState } from "react";
import { getAlerts, send } from "../api.js";
import SecretField from "./SecretField.jsx";
import { REPEATS, agoText } from "../lib.js";

const SENDERS = {
  argus: ["Argus", "Sends to Telegram itself. Nothing else to run."],
  prometheus: ["Prometheus and Alertmanager", "For setups that already run them: Argus exposes /metrics and the alert rules."],
};

function since(view, t) {
  return t ? `${agoText(view.now - t)} ago` : null;
}

// A small form row with its own Save, so each setting saves on its own.
function useAction(csrf, setView, reload) {
  const [busy, setBusy] = useState(null);
  const [note, setNote] = useState(null); // { where, ok, text }
  const act = useCallback(async (where, method, path, body, ok) => {
    setBusy(where);
    setNote(null);
    try {
      const r = await send(method, path, body, csrf);
      if (r.sender !== undefined) setView(r);
      else reload();
      // A test answers 200 with { ok: false, error } when the message did not go.
      if (r.ok === false) setNote({ where, ok: false, text: r.error });
      else if (ok) setNote({ where, ok: true, text: ok });
      return r.ok === false ? null : r;
    } catch (e) {
      setNote({ where, ok: false, text: e.message });
      return null;
    } finally {
      setBusy(null);
    }
  }, [csrf, setView, reload]);
  const say = (where) => note?.where === where && (
    <p className={note.ok ? "form-ok" : "form-error"} role={note.ok ? "status" : "alert"}>{note.text}</p>
  );
  return { act, busy, say, setNote };
}

function Telegram({ view, act, busy, say, handBack }) {
  const [token, setToken] = useState("");
  const [replacing, setReplacing] = useState(false);
  const [chat, setChat] = useState(view.telegram.chat_id);
  const [chats, setChats] = useState(null);
  const t = view.telegram;
  const save = async (e) => {
    e.preventDefault();
    const body = { chat_id: chat };
    if (token) body.telegram_token = token;
    if (await act("tg", "PUT", "/api/alerts", body, "Saved. Now send a test.")) {
      setToken("");
      setReplacing(false);
    }
  };
  const find = async () => {
    if (token) await act("tg", "PUT", "/api/alerts", { telegram_token: token });
    const r = await act("tg", "POST", "/api/alerts/telegram-chats", {});
    if (r) {
      setToken("");
      setReplacing(false);
      setChats(r.chats);
      if (r.chats.length === 1) setChat(r.chats[0].id);
    }
  };
  const tested = view.test_ok_at && since(view, view.test_ok_at);
  return (
    <form className="panel" onSubmit={save}>
      <h2 className="eyebrow">Telegram</h2>
      <ol className="howto">
        <li>In Telegram, message <strong>@BotFather</strong>, send <code>/newbot</code>, and copy the token it gives you.</li>
        <li>Open your new bot and send it any message, so it may write to you.</li>
        <li>Paste the token here and press <em>Find my chat</em>.</li>
      </ol>
      {t.token_set && !replacing ? (
        <p className="field saved-secret">
          <span>Bot token</span>
          <span><span className="st-up">Saved</span>, kept encrypted.{" "}
            <button type="button" className="link-button" onClick={() => setReplacing(true)}>Replace it</button></span>
        </p>
      ) : (
        <label className="field">
          <span>Bot token</span>
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off"
                 placeholder="123456789:AAE..." autoFocus={replacing} />
          <small>Kept encrypted. Argus never shows it again.</small>
        </label>
      )}
      <label className="field">
        <span>Chat ID</span>
        <span className="field-row">
          <input value={chat} onChange={(e) => setChat(e.target.value.trim())} placeholder="123456789"
                 spellCheck="false" />
          <button type="button" className="btn" disabled={busy === "tg" || (!token && !t.token_set)}
                  onClick={find}>Find my chat</button>
        </span>
      </label>
      {chats && chats.length === 0 && (
        <p className="muted">No messages yet. Send your bot a message in Telegram, then try again.</p>
      )}
      {chats && chats.length > 1 && (
        <div className="chat-pick" role="group" aria-label="Chats that messaged the bot">
          {chats.map((c) => (
            <button type="button" key={c.id} className="btn" aria-pressed={chat === c.id}
                    onClick={() => setChat(c.id)}>{c.name} <span className="muted">{c.kind}</span></button>
          ))}
        </div>
      )}
      <div className="actions">
        <button className="btn btn-primary" disabled={busy === "tg"}>Save</button>
        <button type="button" className="btn" disabled={busy === "tg" || !t.token_set || !view.telegram.chat_id}
                onClick={() => act("tg", "POST", "/api/alerts/test", {}, "Sent. Check Telegram.")}>
          Send test
        </button>
        {handBack && (
          <button type="button" className="btn btn-primary" disabled={!view.test_ok_at}
                  title={view.test_ok_at ? "" : "Send a test first"}
                  onClick={() => act("tg", "POST", "/api/alerts/sender", { sender: "argus" },
                    "Argus sends alerts now. Remove Argus's rules from Prometheus, or you will get each alert twice.")}>
            It arrived: Argus takes over
          </button>
        )}
      </div>
      {say("tg")}
      <Delivery view={view} tested={tested} />
    </form>
  );
}

function Delivery({ view, tested }) {
  const d = view.delivery;
  if (d.error && (!d.ok_at || d.error_at > d.ok_at)) {
    return <p className="form-error">Last message failed {since(view, d.error_at)}: {d.error}</p>;
  }
  if (d.ok_at) return <p className="muted small">Last message delivered {since(view, d.ok_at)}.</p>;
  if (tested) return <p className="muted small">Test delivered {tested}.</p>;
  return null;
}

function Prometheus({ view, act, busy, say, handing }) {
  const host = window.location.host;
  const scheme = window.location.protocol.replace(":", "");
  const scrape = `- job_name: argus
  scheme: ${scheme}${scheme === "https" ? `
  tls_config: { insecure_skip_verify: true }   # Argus's own certificate` : ""}
  static_configs:
    - targets: ["${host}"]`;
  return (
    <div className="panel">
      <h2 className="eyebrow">{handing ? "Hand alerts to Prometheus" : "Prometheus sends your alerts"}</h2>
      {handing && <p className="muted">Argus keeps sending until you confirm Prometheus's test arrived, so nothing falls through while you set it up.</p>}
      <ol className="steps-list">
        <li>
          <strong>Scrape Argus.</strong> Add this to <code>prometheus.yml</code>:
          <pre className="snippet">{scrape}</pre>
        </li>
        <li>
          <strong>Load the rules.</strong>{" "}
          <a href="/api/alerts/prometheus-rules.yml" download>Download argus-rules.yml</a> and list it under <code>rule_files</code>.
        </li>
        <li>
          <strong>Route critical alerts to your phone.</strong>{" "}
          <a href="/api/alerts/alertmanager-example.yml" download>An Alertmanager example</a> for Telegram.
        </li>
        <li>
          <strong>Send a test.</strong> Argus raises <code>argus_alert_test</code> for 15 minutes and the <code>ArgusTestAlert</code> rule fires on it.
          <div className="actions">
            <button className="btn" disabled={busy === "prom"}
                    onClick={() => act("prom", "POST", "/api/alerts/prometheus-test", {},
                      "Test started. It should reach you within a few minutes.")}>
              {view.prom_test_active ? "Test running" : "Send a test through Prometheus"}
            </button>
            {handing && (
              <button className="btn btn-primary" disabled={!view.prom_test_at || busy === "prom"}
                      title={view.prom_test_at ? "" : "Send a test first"}
                      onClick={() => act("prom", "POST", "/api/alerts/sender", { sender: "prometheus" },
                        "Prometheus sends alerts now. Argus is quiet.")}>
                It arrived: hand over
              </button>
            )}
          </div>
        </li>
      </ol>
      {say("prom")}
    </div>
  );
}

function WhatPages({ view, act, say }) {
  const rules = view.rules;
  const toggle = (k) => act("rules", "PUT", "/api/alerts", { rules: { ...rules, [k]: !rules[k] } });
  const items = [
    ["checks", "A check goes down, and when it recovers"],
    ["jobs", "A scheduled run fails or is overdue"],
    ["heartbeats", "A timer goes quiet"],
    ["companions", "Any host stops reporting, or a disk on it passes 95% (always pages)"],
  ];
  return (
    <div className="panel">
      <h2 className="eyebrow">What sends a message</h2>
      <p className="muted">Only entries marked <span className="chip chip-critical">pages</span> (severity: critical) send one. The rest show on the dashboard. Problems that start together arrive as one message.</p>
      {items.map(([k, label]) => (
        <label key={k} className="toggle">
          <input type="checkbox" checked={rules[k]} onChange={() => toggle(k)} />
          <span>{label}</span>
        </label>
      ))}
      <label className="field">
        <span>While a problem lasts, remind me</span>
        <select value={view.repeat_minutes}
                onChange={(e) => act("rules", "PUT", "/api/alerts", { repeat_minutes: Number(e.target.value) })}>
          {REPEATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </label>
      <LinkField view={view} act={act} />
      {say("rules")}
    </div>
  );
}

function LinkField({ view, act }) {
  const [link, setLink] = useState(view.link || window.location.origin);
  return (
    <label className="field">
      <span>Link in messages</span>
      <span className="field-row">
        <input value={link} onChange={(e) => setLink(e.target.value)} spellCheck="false" />
        <button type="button" className="btn" disabled={link === view.link}
                onClick={() => act("rules", "PUT", "/api/alerts", { link }, "Saved.")}>Save</button>
      </span>
      <small>Where a tap on a message takes you: the address you open Argus at.</small>
    </label>
  );
}

function Watcher({ view, act, busy, say }) {
  const w = view.watch;
  const save = (value) => act("watch", "PUT", "/api/alerts", { watch_url: value },
    value ? "Saved. Argus pings it every minute." : "Removed.").then((r) => !!r);
  return (
    <div className="panel">
      <h2 className="eyebrow">Who watches Argus</h2>
      <p className="muted">
        A stopped Argus cannot tell you it stopped. Paste a ping URL from a dead-man&apos;s switch
        service (healthchecks.io&apos;s free plan works): Argus pings it every minute while its checks
        run, and the service alerts you when the pings stop.
      </p>
      {!w.url_set && view.sender === "argus" && (
        <p className="banner banner-warn">Nothing watches Argus yet.</p>
      )}
      <SecretField label="Ping URL" saved={w.url_set} placeholder="https://hc-ping.com/..." busy={busy === "watch"}
                   onSave={(v) => save(v)} onRemove={() => save("")} help="Kept encrypted: the URL is the key to your check." />
      {w.error && (!w.ok_at || w.error_at > w.ok_at) ? (
        <p className="form-error">Last ping failed {since(view, w.error_at)}: {w.error}</p>
      ) : w.ok_at ? (
        <p className="muted small">Last ping {since(view, w.ok_at)}.</p>
      ) : null}
      <p className="muted small">
        Or run a second Argus somewhere else and give each a critical check on the other&apos;s <code>/healthz</code>.
      </p>
      {say("watch")}
    </div>
  );
}

export default function AlertsPage({ session }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [want, setWant] = useState(null); // the sender being switched to
  const reload = useCallback(() => getAlerts().then(setView, (e) => setError(e.message)), []);
  const { act, busy, say } = useAction(session.csrf, setView, reload);

  useEffect(() => {
    reload();
    const t = setInterval(reload, 15_000);
    return () => clearInterval(t);
  }, [reload]);
  useEffect(() => { if (view && want === view.sender) setWant(null); }, [view, want]);

  if (error) return <p className="banner banner-error">Could not load the alert settings: {error}</p>;
  if (!view) return <p className="muted pad">Loading…</p>;
  const sender = view.sender;

  return (
    <section className="settings">
      <h1>Alerts</h1>
      <p className="lede">
        Argus tells you when something that matters breaks. Pick who sends the messages; switching
        never leaves a gap.
      </p>

      {view.firing.length > 0 && (
        <div className="panel">
          <h2 className="eyebrow">Open now <span className="count">{view.firing.length}</span></h2>
          <ul className="firing">
            {view.firing.map((f) => (
              <li key={f.key}>
                <span className="dot dot-down" aria-hidden="true" />
                <strong>{f.title}</strong> <span className="muted">{f.detail}</span>
                <span className="muted small">
                  {" "}for {agoText(view.now - f.since)}{f.notified ? "" : sender === "argus" ? ", not sent yet" : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <h2 className="eyebrow">Who sends your alerts?</h2>
      <div className="choice" role="radiogroup" aria-label="Who sends your alerts">
        {Object.entries(SENDERS).map(([k, [name, what]]) => (
          <button key={k} role="radio" aria-checked={sender === k}
                  className={`choice-card ${want === k ? "choice-want" : ""}`}
                  onClick={() => setWant(k === sender ? null : k)}>
            <strong>{name}</strong>
            <span className="muted">{what}</span>
            <span className="choice-state">
              {sender === k ? "Sending now" : want === k ? "Switching: finish the steps below" : "Switch to this"}
            </span>
          </button>
        ))}
      </div>

      {(sender === "argus" || want === "argus") && (
        <Telegram view={view} act={act} busy={busy} say={say} handBack={want === "argus"} />
      )}
      {(sender === "prometheus" || want === "prometheus") && (
        <Prometheus view={view} act={act} busy={busy} say={say} handing={want === "prometheus"} />
      )}

      <WhatPages view={view} act={act} say={say} />
      <Watcher view={view} act={act} busy={busy} say={say} />
    </section>
  );
}
