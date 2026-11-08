import { useCallback, useEffect, useState } from "react";
import { send } from "../api.js";

// Deploy from git, as a guided setup: a repo, a way to write to it, the stacks
// already running moved in (in place, secrets kept in Argus), and Dependabot.
const NEW_REPO = "https://github.com/new?name=homelab-stacks&visibility=private"
  + "&description=" + encodeURIComponent("Docker Compose stacks, deployed by Argus");
const NEW_TOKEN = "https://github.com/settings/personal-access-tokens/new";

function Step({ n, title, done, open, summary, onChange, children }) {
  return (
    <li className={`dstep${done ? " dstep-done" : ""}${open ? " dstep-open" : ""}`}>
      <span className="dstep-n" aria-hidden="true">{done ? "✓" : n}</span>
      <div className="dstep-body">
        <div className="dstep-head">
          <h2>{title}</h2>
          {done && !open && <span className="muted small">{summary}</span>}
          {done && !open && onChange && <button className="link-button small" onClick={onChange}>Change</button>}
        </div>
        {open && children}
      </div>
    </li>
  );
}

function RepoStep({ session, gh, d, onSaved, reloadGh }) {
  const [repo, setRepo] = useState(d.repo || "");
  const [error, setError] = useState(null);
  if (!gh || gh.source === "none") {
    return <><p>Argus reads your repos through GitHub.</p>
      <div className="actions"><a className="btn btn-primary" href="#/github">Connect GitHub</a></div></>;
  }
  const repos = (gh.repos ?? []).filter((r) => !r.archived).map((r) => r.full_name);
  const chosen = (gh.installations ?? []).filter((i) => i.selection !== "all");
  const save = async () => {
    setError(null);
    try { onSaved(await send("PUT", "/api/deploys", { repo }, session.csrf)); } catch (e) { setError(e.message); }
  };
  return (
    <>
      <p className="muted">One private repo holds a folder per stack. A new, empty repo is best; Argus fills it in.</p>
      <div className="actions">
        <a className="btn btn-primary" href={NEW_REPO} target="_blank" rel="noreferrer">Create one on GitHub</a>
        <span className="muted small">named homelab-stacks, private; press Create, then come back.</span>
      </div>
      <div className="field-row">
        <select value={repo} onChange={(e) => setRepo(e.target.value)} aria-label="Repo">
          <option value="">Choose the repo…</option>
          {repos.map((r) => <option key={r}>{r}</option>)}
        </select>
        <button className="btn" onClick={reloadGh}>Look again</button>
        <button className="btn btn-primary" disabled={!repo} onClick={save}>Use it</button>
      </div>
      {chosen.length > 0 && (
        <p className="small muted">Not in the list? Argus's GitHub app only sees the repos you gave it:{" "}
          {chosen.map((i) => <a key={i.account} href={i.url} target="_blank" rel="noreferrer">add it for {i.account}</a>)}, then Look again.</p>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </>
  );
}

function TokenStep({ session, d, onSaved }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState(null);
  const save = async (e) => {
    e.preventDefault();
    setError(null);
    try { onSaved(await send("PUT", "/api/deploys", { write_token: token }, session.csrf)); setToken(""); }
    catch (err) { setError(err.message); }
  };
  return (
    <form onSubmit={save} className="dform">
      <p className="muted">Argus commits your compose files to <strong>{d.repo}</strong>, and nothing else. GitHub needs a token for that one repo:</p>
      <ol className="dlist">
        <li><a href={NEW_TOKEN} target="_blank" rel="noreferrer">Open GitHub's new token page</a>. Name it <code>argus deploys</code>; pick an expiry you are happy with.</li>
        <li>Repository access: <strong>Only select repositories</strong>, then <strong>{d.repo}</strong>.</li>
        <li>Permissions, Repository: <strong>Contents</strong>, <strong>Read and write</strong>. Nothing else.</li>
        <li>Generate it, copy it, paste it here.</li>
      </ol>
      <div className="field-row">
        <input type="password" value={token} onChange={(e) => setToken(e.target.value.trim())} autoComplete="off"
               placeholder="github_pat_…" aria-label="Token" />
        <button className="btn btn-primary" disabled={!token}>Save</button>
      </div>
      <p className="small muted">Kept encrypted in Argus. Argus checks it can push to {d.repo} before keeping it.</p>
      {error && <p className="form-error" role="alert">{error}</p>}
    </form>
  );
}

// hosts/ files that name a host Argus does not know: an old name, or a host gone.
function Unmatched({ session, d, hosts, onSaved }) {
  const [pick, setPick] = useState(() => Object.fromEntries(d.unmatched.map((u) => [u.host, u.suggest ?? ""])));
  const [error, setError] = useState(null);
  const act = async (old, nu) => {
    setError(null);
    try { onSaved(await send("POST", "/api/deploys/hosts", { old, new: nu || null }, session.csrf)); }
    catch (e) { setError(e.message); }
  };
  if (!d.unmatched.length) return null;
  return (
    <div className="dhost">
      <h3 className="eyebrow">names in the repo Argus does not know</h3>
      <p className="small muted">Your repo lists stacks for these, but no host in Argus has the name. Tell Argus which host each is, and it renames the file in the repo.</p>
      <ul className="rows">{d.unmatched.map((u) => (
        <li key={u.host}>
          <span className="dot dot-failing" />
          <span className="row-main mono">hosts/{u.host}<span className="row-sub muted">{u.stacks.length} stack{u.stacks.length === 1 ? "" : "s"}: {u.stacks.map((x) => x.split("/").pop()).join(", ")}</span></span>
          <span className="row-note">
            <select value={pick[u.host]} onChange={(e) => setPick({ ...pick, [u.host]: e.target.value })} aria-label={`Host for ${u.host}`}>
              <option value="">Not a host any more</option>
              {hosts.map((h) => <option key={h.name} value={h.name}>{h.name}</option>)}
            </select></span>
          <span className="row-act">
            <button className={`btn btn-small${pick[u.host] ? " btn-primary" : ""}`} onClick={() => act(u.host, pick[u.host])}>
              {pick[u.host] ? `Rename to ${pick[u.host]}` : "Remove from the repo"}</button></span>
        </li>
      ))}</ul>
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

// Private images (ghcr.io/you/...): hosts need a read token to pull them, or
// their first deploy fails with "denied".
function PullToken({ session, d, onSaved }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState(null);
  if (d.pull_token_set) return null;
  const save = async (e) => {
    e.preventDefault();
    setError(null);
    try { onSaved(await send("PUT", "/api/deploys", { pull_token: token }, session.csrf)); setToken(""); }
    catch (err) { setError(err.message); }
  };
  return (
    <form className="dform dnote" onSubmit={save}>
      <p className="small"><strong>Private images?</strong> If only you can pull some images (<code>ghcr.io/you/…</code>), hosts need a read token.
        Make one on GitHub under Settings, Developer settings, Personal access tokens, <strong>Tokens (classic)</strong>,
        with only <code>read:packages</code>. Skip this if every image is public.</p>
      <div className="field-row">
        <input type="password" value={token} onChange={(e) => setToken(e.target.value.trim())} autoComplete="off"
               placeholder="ghp_…" aria-label="Read token for private images" />
        <button className="btn" disabled={!token}>Save</button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
    </form>
  );
}

const ROW = { reading: "reading its files…", checking: "checking…", committing: "saving…", done: "deployed from git now",
  blocked: "needs a fix", failed: "failed" };

function MoveStep({ session, hosts, d, onSaved, onDone }) {
  // Argus's own stack stays out: deploying itself would restart it mid-deploy; it updates from Settings.
  const isArgus = (h, p) => (h.report?.containers ?? []).some((c) => c.project === p.project && c.image.startsWith("ghcr.io/devinmb/argus"));
  const projects = hosts.flatMap((h) => (h.adoptable ?? []).filter((p) => !isArgus(h, p))
    .map((p) => ({ ...p, host: h.name, key: `${h.name}/${p.project}` })));
  const [picked, setPicked] = useState(() => new Set(projects.map((p) => p.key)));
  const [rows, setRows] = useState({});
  const [running, setRunning] = useState(false);
  const set = (k, v) => setRows((r) => ({ ...r, [k]: { ...r[k], ...v } }));
  const go = async () => {
    setRunning(true);
    const todo = projects.filter((p) => picked.has(p.key));
    // Ask every host for its files at once; each answers on its next report.
    await Promise.all(todo.map(async (p) => {
      set(p.key, { state: "reading" });
      try { await send("POST", `/api/adopt/${p.host}/${encodeURIComponent(p.project)}/read`, {}, session.csrf); }
      catch (e) { set(p.key, { state: "failed", text: e.message }); }
    }));
    for (const p of todo) {
      let pv = null;
      for (let i = 0; i < 60; i++) {
        pv = await send("GET", `/api/adopt/${p.host}/${encodeURIComponent(p.project)}`).catch(() => null);
        if (pv && pv.state !== "collecting" && pv.state !== "none") break;
        await new Promise((r) => setTimeout(r, 3000));
      }
      if (!pv || pv.state !== "ready") { set(p.key, { state: "failed", text: pv?.error || "the host did not send its files" }); continue; }
      if (pv.blockers.length) { set(p.key, { state: "blocked", text: pv.blockers.map((b) => b.text).join(" ") }); continue; }
      set(p.key, { state: "committing", secrets: pv.secrets.length });
      try {
        await send("POST", `/api/adopt/${p.host}/${encodeURIComponent(p.project)}`, {}, session.csrf);
        set(p.key, { state: "done" });
      } catch (e) { set(p.key, { state: "failed", text: e.message }); }
    }
    setRunning(false);
    onDone();
  };
  if (!projects.length && !Object.keys(rows).length) {
    return (
      <>
        <Unmatched session={session} d={d} hosts={hosts} onSaved={onSaved} />
        <p className="muted">Nothing left to move: every Compose project on your hosts is deployed from git.</p>
      </>
    );
  }
  const links = projects.filter((p) => p.link).length;
  // Match old names first, or their stacks would be copied in under new names.
  const unmatchedFirst = (d.unmatched ?? []).some((u) => u.suggest);
  const byHost = [...new Set(projects.map((p) => p.host))];
  return (
    <>
      <Unmatched session={session} d={d} hosts={hosts} onSaved={onSaved} />
      <p className="muted">These run on your hosts today. Those already in the repo are linked: Argus takes over deploying them where they run. The rest move in place: its compose file goes to git, its <code>.env</code> secrets stay
        in Argus, and its containers keep running where they are. No restart, no data moved. Argus itself stays out; it updates from Settings.</p>
      {byHost.map((h) => (
        <div key={h} className="dhost">
          <h3 className="eyebrow">{h}</h3>
          <ul className="rows">{projects.filter((p) => p.host === h).map((p) => {
            const r = rows[p.key];
            return (
              <li key={p.key}>
                <input type="checkbox" checked={picked.has(p.key)} disabled={running || !!r}
                       onChange={(e) => setPicked((s) => { const n = new Set(s); e.target.checked ? n.add(p.key) : n.delete(p.key); return n; })}
                       aria-label={p.project} />
                <span className="row-main">{p.project}{p.link && <span className="chip">in the repo: link</span>}
                  <span className="row-sub muted">{p.containers.length} container{p.containers.length === 1 ? "" : "s"} · <span className="mono">{p.dir}</span></span></span>
                <span className={`row-note ${r?.state === "done" ? "st-up" : ["blocked", "failed"].includes(r?.state) ? "st-failing" : "muted"}`}>
                  {r ? ROW[r.state] : ""}</span>
                <span className="row-act" />
                {r?.text && <p className="row-wide small muted">{r.text}</p>}
              </li>
            );
          })}</ul>
        </div>
      ))}
      <PullToken session={session} d={d} onSaved={onSaved} />
      <div className="actions">
        <button className="btn btn-primary" disabled={running || unmatchedFirst || picked.size === 0 || Object.keys(rows).length > 0} onClick={go}>
          {running ? "Working…" : links ? `Link and move ${picked.size}` : `Move ${picked.size} into git`}</button>
        <span className="muted small">{unmatchedFirst ? "Match the names above first." : "Hosts send their files within a minute."}</span>
      </div>
    </>
  );
}

function UpdatesStep({ session, d }) {
  const [state, setState] = useState(null);
  const turnOn = async () => {
    setState("busy");
    try { const r = await send("POST", "/api/deploys/dependabot", {}, session.csrf); setState(r.existing ? "existing" : "on"); }
    catch (e) { setState(e.message); }
  };
  return (
    <>
      <p>From now on, change a stack by merging in <strong>{d.repo}</strong>: Argus deploys it to the host that runs it, within that host's own rules.</p>
      <p className="muted">Want new image versions to come to you? Dependabot opens a pull request for each one; merging it deploys it.</p>
      <div className="actions">
        {state === "existing" ? <span className="st-up">{d.repo} already has a Dependabot config; Argus left it as it is.</span>
          : state === "on" ? <span className="st-up">Dependabot is on in {d.repo}.</span>
          : <button className="btn btn-primary" disabled={state === "busy"} onClick={turnOn}>Turn on Dependabot</button>}
        <a className="btn" href="#/hosts">Done</a>
      </div>
      {state && !["busy", "on", "existing"].includes(state) && <p className="form-error" role="alert">{state}</p>}
    </>
  );
}

export default function DeploySetup({ session, data, reload }) {
  const [d, setD] = useState(null);
  const [gh, setGh] = useState(null);
  const [editing, setEditing] = useState(null);
  const [moved, setMoved] = useState(false);
  const loadGh = useCallback((fresh) => (fresh ? send("POST", "/api/github/refresh", {}, session.csrf) : send("GET", "/api/github"))
    .then(setGh, () => setGh(null)), [session.csrf]);
  useEffect(() => {
    send("GET", "/api/deploys").then(setD, () => {});
    loadGh(false);
  }, [loadGh]);
  if (!d) return <section className="settings"><h1>Deploy from git</h1><p className="muted">Loading…</p></section>;
  const hosts = data?.companions?.companions ?? [];
  const inGit = Object.values(d.hosts ?? {}).reduce((n, s) => n + s.length, 0);
  // Step 3 is done when nothing is left: no project outside git (Argus itself
  // aside), and no hosts/ file under a name Argus does not know.
  const left = hosts.reduce((n, h) => n + (h.adoptable ?? []).filter((p) => !(h.report?.containers ?? [])
    .some((c) => c.project === p.project && c.image.startsWith("ghcr.io/devinmb/argus"))).length, 0);
  const steps = [!!d.repo, d.write_token_set, inGit > 0 && left === 0 && !(d.unmatched ?? []).length];
  const current = editing ?? steps.findIndex((s) => !s);
  const at = current === -1 ? 3 : current;
  const saved = (v) => { setD(v); setEditing(null); };
  return (
    <section className="settings deploy-setup">
      <h1>Deploy from git</h1>
      <p className="lede">Keep your Docker Compose files in GitHub. Argus deploys them to your hosts, and when a change is merged, it deploys itself.</p>
      <ol className="dsteps">
        <Step n={1} title="Where your stacks live" done={steps[0]} open={at === 0} summary={d.repo} onChange={() => setEditing(0)}>
          <RepoStep session={session} gh={gh} d={d} onSaved={saved} reloadGh={() => loadGh(true)} />
        </Step>
        <Step n={2} title="Let Argus write to it" done={steps[1]} open={at === 1} summary="token saved" onChange={() => setEditing(1)}>
          <TokenStep session={session} d={d} onSaved={saved} />
        </Step>
        <Step n={3} title="Move your stacks in" done={steps[2]} open={at === 2} summary={`${inGit} stack${inGit === 1 ? "" : "s"} in git, nothing left`}
              onChange={() => setEditing(2)}>
          <MoveStep session={session} hosts={hosts} d={{ ...d, unmatched: d.unmatched ?? [] }}
                    onSaved={(v) => { setD(v); reload(); }}
                    onDone={() => { setMoved(true); reload(); send("GET", "/api/deploys").then(setD, () => {}); }} />
          {moved && <p><button className="btn" onClick={() => setEditing(null)}>Next</button></p>}
        </Step>
        <Step n={4} title="Keep them up to date" done={false} open={at === 3}>
          <UpdatesStep session={session} d={d} />
        </Step>
      </ol>
      {d.error && <p className="banner banner-warn">{d.error}</p>}
    </section>
  );
}
