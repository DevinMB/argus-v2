import { useCallback, useEffect, useState } from "react";
import { send } from "../api.js";
import { agoText } from "../lib.js";

// Leave Argus (design decision 18): a backup to come back with, every
// companion uninstalled with its stacks handed back, then what lives outside
// Argus, for the owner to remove or keep. Argus deletes nothing outside itself.
function HostState({ h }) {
  if (h.read_only) return <span className="small muted">monitoring only: run <code>sudo /usr/local/bin/argus-companion uninstall</code> on it, then Remove</span>;
  const u = h.uninstall;
  if (!u) return <span className="small muted">still installed</span>;
  if (u.state === "failed") return <span className="small st-failing">handback failed: {u.log || "see its page"}</span>;
  if (u.stale) return <span className="small st-failing">no answer: run <code>{u.command}</code> on it</span>;
  return <span className="small st-accent">uninstalling, waiting for its next report</span>;
}

export default function LeavePage({ session }) {
  const [v, setV] = useState(null);
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const load = useCallback(() => send("GET", "/api/leave").then(setV, (e) => setError(e.message)), []);
  useEffect(() => {
    load();
    const t = setInterval(load, 10000); // hosts drop off as they uninstall
    return () => clearInterval(t);
  }, [load]);
  if (!v) return <section className="settings"><h1>Leave Argus</h1><p className="muted">{error ?? "Loading…"}</p></section>;
  const left = v.hosts.filter((h) => !h.read_only && !h.uninstall);
  const go = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await send("POST", "/api/companions/uninstall", { password, again }, session.csrf);
      setPassword(""); setAgain("");
      load();
    } catch (err) { setError(err.message); }
    setBusy(false);
  };
  return (
    <section className="settings leave">
      <a className="small muted" href="#/account">← Account</a>
      <h1>Leave Argus</h1>
      <p className="lede muted">Take your stacks with you, remove Argus from every host, and keep a way back.
        Nothing outside Argus is deleted for you.</p>

      <div className="panel">
        <h2 className="eyebrow">1. Keep a way back</h2>
        <p>Export a backup first. A new Argus restored from it has your settings, secrets, hosts and checks; each host
          then runs Add a host again, and its stacks in <code>/opt/stacks</code> are taken over where they are.</p>
        <div className="actions"><a className="btn" href="#/account">Export a backup</a></div>
      </div>

      <div className="panel">
        <h2 className="eyebrow">2. Uninstall every host</h2>
        <p>Each companion moves the stacks Argus deployed to <code>/opt/stacks/&lt;name&gt;</code>, compose file and
          <code> .env</code> together, then removes itself. Running containers keep running.</p>
        {v.hosts.length > 0 ? (
          <ul className="rows">{v.hosts.map((h) => (
            <li key={h.id}><span className="row-main">{h.name}</span><span className="row-note"><HostState h={h} /></span><span className="row-act" /></li>
          ))}</ul>
        ) : <p className="small st-up">No hosts left in Argus.</p>}
        {v.uninstalled.length > 0 && (
          <ul className="rows uninstalled">{v.uninstalled.map((u) => (
            <li key={`${u.name}-${u.at}`}><span className="row-main">{u.name}<span className="row-sub muted">{u.log}</span></span>
              <span className="row-note small st-up">uninstalled {agoText(Date.now() / 1000 - u.at)}</span><span className="row-act" /></li>
          ))}</ul>
        )}
        {left.length > 0 && (
          <form onSubmit={go} className="uninstall">
            <div className="form-grid">
              <label className="field"><span>Admin password</span>
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required /></label>
              <label className="field"><span>The password again</span>
                <input type="password" value={again} onChange={(e) => setAgain(e.target.value)} autoComplete="current-password" required /></label>
            </div>
            <div className="actions">
              <button className="btn btn-danger" disabled={busy || !password || password !== again}>
                {busy ? "Asking…" : `Uninstall from ${left.length} host${left.length === 1 ? "" : "s"}`}</button>
              {password && again && password !== again && <span className="small st-failing">The two passwords are not the same.</span>}
            </div>
          </form>
        )}
        {error && <p className="form-error" role="alert">{error}</p>}
      </div>

      <div className="panel">
        <h2 className="eyebrow">3. What lives outside Argus</h2>
        {v.outside.length ? (
          <ul className="rows">{v.outside.map((o) => (
            <li key={o.key}><span className="row-main">{o.title}<span className="row-sub muted">{o.how}</span></span>
              <span className="row-note" />
              <span className="row-act">{o.url && <a className="btn btn-small" href={o.url} target="_blank" rel="noreferrer">Open</a>}</span></li>
          ))}</ul>
        ) : <p className="small muted">Nothing: Argus set up nothing outside itself.</p>}
      </div>

      <div className="panel">
        <h2 className="eyebrow">4. Argus itself</h2>
        <p>Last, on {v.argus_host ?? "the machine Argus runs on"}, in the folder with Argus&apos;s compose.yml:</p>
        <pre className="snippet command">docker compose down -v</pre>
        <p className="small muted"><code>-v</code> deletes Argus&apos;s data volume. After that, the export from step 1 is the only way back.</p>
      </div>
    </section>
  );
}
