import { useCallback, useEffect, useState } from "react";
import { send } from "../api.js";
import { agoText, safeHref } from "../lib.js";

const SAVED = {
  argus: () => "Saved. It applies now.",
  commit: (url) => <>Committed to git (<a href={safeHref(url)} target="_blank" rel="noreferrer">the commit</a>); it applies now.</>,
  pr: (url) => <>Opened a <a href={safeHref(url)} target="_blank" rel="noreferrer">pull request</a>; it applies once merged.</>,
};

export function savedText(out) {
  return out?.saved ? SAVED[out.saved](out.url) : null;
}

// Add or change one check. `check` is the existing one, or null for a new one.
export function CheckForm({ session, check, initial, sites, groups, hosts, onDone, onCancel }) {
  const [f, setF] = useState(() => ({
    ...{
    name: check?.name ?? "", kind: check?.kind ?? "http", target: check?.target ?? "",
    group: check?.group ?? "default", site: check?.site ?? Object.keys(sites)[0] ?? "",
    severity: check?.severity ?? "warning", interval: check?.interval ?? 60,
    companion: check?.companion ?? "",
    },
    ...initial, // a suggestion: filled in, still a new check
  }));
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const hint = { http: "https://app.lan/health", tcp: "db.lan:5432", ping: "nas.lan" }[f.kind];

  const save = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const config = await send("GET", "/api/config");
      const entry = { ...f, interval: Number(f.interval) };
      if (!entry.companion) delete entry.companion;
      if (Object.keys(sites).length <= 1) delete entry.site;
      if (check) {
        // Keep what the form does not show (timeouts, expected statuses, notes).
        for (const k of ["timeout", "expect_status", "verify_tls", "description"]) {
          if (check[k] !== undefined) entry[k] = check[k];
        }
      }
      onDone(await send("POST", "/api/config/entries",
        { kind: "checks", entry, replace: check?.name ?? null, base: config.base }, session.csrf));
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };

  return (
    <form className="panel check-form" onSubmit={save}>
      <h2 className="eyebrow">{check ? `Change ${check.name}` : "Add a check"}</h2>
      <div className="form-grid">
        <label className="field"><span>Name</span>
          <input value={f.name} onChange={set("name")} required pattern="[a-z0-9][a-z0-9\-]{0,62}"
                 disabled={!!check} placeholder="jellyfin" spellCheck="false" /></label>
        <label className="field"><span>Kind</span>
          <select value={f.kind} onChange={set("kind")}>
            <option value="http">HTTP: a page answers</option>
            <option value="tcp">TCP: a port is open</option>
            <option value="ping">Ping: the host is up</option>
          </select></label>
        <label className="field wide"><span>Target</span>
          <input value={f.target} onChange={set("target")} required placeholder={hint} spellCheck="false" /></label>
        <label className="field"><span>Group</span>
          <input value={f.group} onChange={set("group")} list="groups" spellCheck="false" />
          <datalist id="groups">{groups.map((g) => <option key={g} value={g} />)}</datalist></label>
        <label className="field"><span>When it is down</span>
          <select value={f.severity} onChange={set("severity")}>
            <option value="warning">Dashboard only</option>
            <option value="critical">Alert me</option>
          </select></label>
        <label className="field"><span>Every</span>
          <select value={f.interval} onChange={set("interval")}>
            {[30, 60, 120, 300, 900].map((s) => <option key={s} value={s}>{s < 120 ? `${s} seconds` : `${s / 60} minutes`}</option>)}
          </select></label>
        <label className="field"><span>Checked from</span>
          <select value={f.companion} onChange={set("companion")}>
            <option value="">Argus</option>
            {hosts.map((h) => <option key={h} value={h}>inside {h}</option>)}
          </select></label>
        {Object.keys(sites).length > 1 && (
          <label className="field"><span>Site</span>
            <select value={f.site} onChange={set("site")}>
              {Object.entries(sites).map(([id, n]) => <option key={id} value={id}>{n}</option>)}
            </select></label>
        )}
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="actions">
        <button className="btn btn-primary" disabled={busy}>{busy ? "Saving…" : check ? "Save" : "Add"}</button>
        <button className="btn" type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

// Where the config lives, and the whole of it as YAML.
export default function ConfigPage({ session }) {
  const [view, setView] = useState(null);
  const [text, setText] = useState("");
  const [repos, setRepos] = useState([]);
  const [git, setGit] = useState({ repo: "", path: "argus.yml", branch: "", mode: "commit" });
  const [msg, setMsg] = useState(null);
  const [error, setError] = useState(null);
  const [wantGit, setWantGit] = useState(false);
  const load = useCallback(async () => {
    const v = await send("GET", "/api/config");
    setView(v);
    setText(v.text);
    // Keep what was typed when the config is not in git yet, so a refused move can be fixed.
    if (v.source === "github") setGit({ repo: v.repo, path: v.path, branch: v.branch, mode: v.mode });
  }, []);
  useEffect(() => {
    load().catch((e) => setError(e.message));
    send("GET", "/api/github").then((g) => setRepos(g.repos.filter((r) => !r.archived)), () => {});
  }, [load]);
  if (!view) return <section className="settings"><h1>Config</h1><p className="muted">{error ?? "Loading…"}</p></section>;

  const act = async (fn) => {
    setError(null);
    setMsg(null);
    try {
      const out = await fn();
      setView(out);
      setText(out.text);
      setMsg(savedText(out) ?? "Done.");
    } catch (e) {
      setError(e.message);
      if (e.status === 409) load();
    }
  };
  const move = (source) => act(() => send("PUT", "/api/config/source", { source, ...git }, session.csrf));
  const dirty = text !== view.text;

  return (
    <section className="settings">
      <h1>Config</h1>
      <p className="lede">
        Every check, timer and run Argus watches. Change single checks on the Checks page; this page
        holds the whole file.
      </p>

      <div className="panel">
        <h2 className="eyebrow">Where it lives</h2>
        <label className="toggle"><input type="radio" checked={view.source === "argus" && !wantGit}
                                         onChange={() => { setWantGit(false); move("argus"); }} />
          <span><strong>In Argus</strong><small>Saved in Argus's database. Export it any time.</small></span></label>
        <label className="toggle"><input type="radio" checked={view.source === "github" || wantGit}
                                         onChange={() => setWantGit(true)} />
          <span><strong>In a GitHub repo</strong>
            <small>Changes made in git show here within a minute; edits here become a commit or a pull request.</small></span></label>
        {(view.source === "github" || wantGit) && (
          <div className="form-grid">
            <label className="field"><span>Repo</span>
              <select value={git.repo} onChange={(e) => setGit({ ...git, repo: e.target.value })}>
                <option value="">Choose…</option>
                {repos.map((r) => <option key={r.full_name}>{r.full_name}</option>)}
              </select></label>
            <label className="field"><span>File</span>
              <input value={git.path} onChange={(e) => setGit({ ...git, path: e.target.value })} spellCheck="false" /></label>
            <label className="field"><span>Edits become</span>
              <select value={git.mode} onChange={(e) => setGit({ ...git, mode: e.target.value })}>
                <option value="commit">a commit</option>
                <option value="pr">a pull request to review</option>
              </select></label>
            <div className="actions">
              <button className="btn btn-primary" disabled={!git.repo} onClick={() => move("github")}>
                {view.source === "github" ? "Save" : "Use this repo"}
              </button>
            </div>
            {!view.write_token_set && (
              <small className="st-failing wide">Saving edits to git needs the write token (GitHub page, Deploys). Reading works without it.</small>
            )}
          </div>
        )}
        {view.source === "file" && (
          <p className="small">Now: the mounted file <code>{view.file}</code>. Edit it there; this page only shows it.</p>
        )}
        {view.read_at && <p className="small muted">Read from git {agoText(Date.now() / 1000 - view.read_at)}.</p>}
        {view.error && <p className="form-error">{view.error}</p>}
      </div>

      <div className="panel">
        <h2 className="eyebrow">argus.yml</h2>
        <textarea className="yaml" value={text} onChange={(e) => setText(e.target.value)} readOnly={!view.editable}
                  spellCheck="false" aria-label="argus.yml" rows={Math.min(40, text.split("\n").length + 2)} />
        <div className="actions">
          {view.editable && (
            <button className="btn btn-primary" disabled={!dirty}
                    onClick={() => act(() => send("PUT", "/api/config", { text, base: view.base }, session.csrf))}>
              Save
            </button>
          )}
          {dirty && <button className="btn" onClick={() => setText(view.text)}>Discard changes</button>}
          <a className="btn" href="/api/config/export.yml">Export</a>
        </div>
        <small className="muted">Argus checks the whole file before saving; a mistake is shown here and nothing changes.</small>
      </div>
      {msg && <p className="form-ok" role="status">{msg}</p>}
      {error && <p className="banner banner-error" role="alert">{error}</p>}
    </section>
  );
}
