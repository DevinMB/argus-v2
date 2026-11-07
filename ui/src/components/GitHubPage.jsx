import { useCallback, useEffect, useState } from "react";
import { send } from "../api.js";
import SecretField from "./SecretField.jsx";
import { agoText, safeHref } from "../lib.js";

// GitHub app names are unique across GitHub ("Argus" is taken), so the default
// carries the account it is made for: "Argus for octocat".
export const appName = (owner) => (owner ? `Argus for ${owner}` : "Argus").slice(0, 34);

function Ext({ href, children }) {
  const safe = safeHref(href);
  return safe ? <a href={safe} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>;
}

// "Connect GitHub": Argus makes its own GitHub App through GitHub's manifest flow.
function Connect({ session, setError }) {
  const [owner, setOwner] = useState(""); // your username, or the organization
  const [custom, setCustom] = useState(null); // a name typed by hand wins
  const [where, setWhere] = useState("me");
  const name = custom ?? appName(owner);
  const org = where === "org" ? owner : "";
  const [busy, setBusy] = useState(false);
  const go = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const m = await send("POST", "/api/github/manifest",
        { url: window.location.origin, name, org }, session.csrf);
      // GitHub takes the manifest as a form post; the browser goes there and back.
      const form = document.createElement("form");
      form.method = "post";
      form.action = m.action;
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "manifest";
      input.value = m.manifest;
      form.appendChild(input);
      document.body.appendChild(form);
      form.submit();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };
  return (
    <form className="panel" onSubmit={go}>
      <h2 className="eyebrow">Connect GitHub</h2>
      <p>
        Argus makes its own GitHub App on your account, in two clicks on GitHub. Then you choose
        which repos it may see. It can only read: pull requests, CI runs, security alerts and code.
        Nothing needs to reach Argus from the internet; it asks GitHub every minute.
      </p>
      <div className="field">
        <span>Make it under</span>
        <label className="toggle"><input type="radio" checked={where === "me"} onChange={() => setWhere("me")} />
          <span>My personal account</span></label>
        <label className="toggle"><input type="radio" checked={where === "org"} onChange={() => setWhere("org")} />
          <span>An organization I own</span></label>
      </div>
      <label className="field">
        <span>{where === "org" ? "Organization" : "Your GitHub username"}</span>
        <input value={owner} onChange={(e) => setOwner(e.target.value.trim())} required
               pattern="[A-Za-z0-9\-]{1,39}" spellCheck="false" autoComplete="username"
               placeholder={where === "org" ? "my-org" : "octocat"} />
      </label>
      <label className="field">
        <span>App name</span>
        <input value={name} onChange={(e) => setCustom(e.target.value)} required minLength={3} maxLength={34} />
        <small>GitHub app names are unique across GitHub, so it carries your name. Taken anyway? Change it here and try again.</small>
      </label>
      <div className="actions">
        <button className="btn btn-primary" disabled={busy}>{busy ? "Going to GitHub…" : "Connect GitHub"}</button>
      </div>
    </form>
  );
}

function PasteToken({ session, view, onView, setError }) {
  const [busy, setBusy] = useState(false);
  const save = async (value) => {
    setBusy(true);
    setError(null);
    try {
      onView(await send("PUT", "/api/github", { token: value }, session.csrf));
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    } finally {
      setBusy(false);
    }
  };
  return (
    <details className="panel" open={view.source === "token"}>
      <summary className="eyebrow">{view.source === "token" ? "The pasted token" : "Or paste a token instead"}</summary>
      <p className="muted">
        For accounts that cannot make apps. On GitHub: Settings, Developer settings, Fine-grained
        tokens. Choose the repos, and give it read-only access to Actions, Contents, Dependabot
        alerts, Metadata and Pull requests. Argus keeps it encrypted and never shows it again.
      </p>
      <SecretField label="GitHub token" saved={view.token_set} placeholder="github_pat_…" busy={busy}
                   onSave={save} onRemove={() => save("")} />
    </details>
  );
}

function Repos({ session, view, onView, setError }) {
  const shown = view.repos.filter((r) => !r.archived && !r.hidden).length;
  const toggle = async (r) => {
    const hidden = view.repos.filter((x) => x.hidden).map((x) => x.full_name);
    const next = r.hidden ? hidden.filter((n) => n !== r.full_name) : [...hidden, r.full_name];
    try {
      onView(await send("PUT", "/api/github", { hidden: next }, session.csrf));
    } catch (err) {
      setError(err.message);
    }
  };
  return (
    <div className="panel">
      <h2 className="eyebrow">Repos <span className="count">{shown} shown</span></h2>
      {view.found_error && <p className="form-error">Listing repos failed: {view.found_error}</p>}
      {view.repos.length === 0 && !view.found_error && (
        <p className="muted">No repos yet. {view.source === "app" && "Install the app on the repos to watch."}</p>
      )}
      <ul className="repo-list">
        {view.repos.map((r) => (
          <li key={r.full_name}>
            <label className="toggle">
              <input type="checkbox" checked={!r.hidden && !r.archived} disabled={r.archived}
                     onChange={() => toggle(r)} />
              <span>
                <strong>{r.full_name}</strong>
                <small>{r.archived ? "archived: never shown" : r.private ? "private" : "public"}</small>
              </span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  );
}

// Where deploys come from: one repo with hosts/<host> files listing stack folders.
function Deploys({ session, view, setError }) {
  const [d, setD] = useState(null);
  useEffect(() => { send("GET", "/api/deploys").then(setD, (e) => setError(e.message)); }, [setError]);
  if (!d) return null;
  const save = async (body) => {
    setError(null);
    try {
      setD(await send("PUT", "/api/deploys", body, session.csrf));
      return true;
    } catch (e) {
      setError(e.message);
      return false;
    }
  };
  const folder = d.source?.kind === "folder";
  const hosts = Object.entries(d.hosts ?? {});
  return (
    <div className="panel">
      <h2 className="eyebrow">Deploys</h2>
      <p><a className="btn btn-small btn-primary" href="#/deploy">Set up deploys, step by step</a></p>
      <p className="muted">
        One repo says what each host runs: <code>hosts/&lt;host&gt;</code> lists stack folders, and each
        folder holds a <code>compose.yml</code>. A host's companion deploys its stacks when they change,
        within the host's own policy.
      </p>
      {folder ? (
        <p>From the folder <code>{d.source.where}</code> (ARGUS_DEPLOY_DIR).</p>
      ) : (
        <label className="field">
          <span>Deploy repo</span>
          <select value={d.repo} onChange={(e) => save({ repo: e.target.value })}>
            <option value="">None: no deploys</option>
            {view.repos.filter((r) => !r.archived).map((r) => <option key={r.full_name}>{r.full_name}</option>)}
          </select>
        </label>
      )}
      {d.error && <p className="form-error">{d.error}</p>}
      {d.commit && (
        <p className="small">
          At {d.commit}, read {agoText(Date.now() / 1000 - d.read_at)}.{" "}
          {hosts.length ? hosts.map(([h, s]) => `${h}: ${s.length}`).join(" · ") : "No hosts/ files yet."}
        </p>
      )}
      {!folder && d.repo && (
        <>
          <SecretField label="Write token (tag bumps and moving stacks in)" saved={d.write_token_set} placeholder="github_pat_…"
                       onSave={(v) => save({ write_token: v })} onRemove={() => save({ write_token: "" })}
                       help={<>A fine-grained token for <strong>{d.repo} only</strong>, with Contents: read and write. Argus writes new
                         image tags and moved-in stacks there; its GitHub app stays read-only everywhere else.</>} />
          {!d.write_token_set && <p className="small st-failing">Off: tags are not bumped and stacks cannot move in until a token is saved.</p>}
        </>
      )}
      <SecretField label="Read token for private images" saved={d.pull_token_set} placeholder="ghp_…"
                   onSave={(v) => save({ pull_token: v })} onRemove={() => save({ pull_token: "" })}
                   help={<>Needed when your images are private (ghcr.io/you/…): hosts pull with it, sent only with a deploy and
                     never stored on the host. A classic token with only <code>read:packages</code>.</>} />
    </div>
  );
}

export default function GitHubPage({ session }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [confirm, setConfirm] = useState(false);
  const load = useCallback(async (refresh) => {
    try {
      setView(await (refresh ? send("POST", "/api/github/refresh", {}, session.csrf)
        : send("GET", "/api/github")));
    } catch (err) {
      setError(err.message);
    }
  }, [session.csrf]);
  // Back from installing the app on GitHub: look for the new repos at once.
  useEffect(() => { load(window.location.hash.includes("installation_id")); }, [load]);
  if (!view) return <section className="settings"><h1>GitHub</h1><p className="muted">{error ?? "Loading…"}</p></section>;

  const disconnect = async () => {
    try {
      setView(await send("POST", "/api/github/disconnect", {}, session.csrf));
      setConfirm(false);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <section className="settings">
      <h1>GitHub</h1>
      <p className="lede">
        Argus reads each repo's update PRs, CI runs and security alerts, and shows where every
        change is on its way to running. <a href="#/updates">Open Updates</a>.
      </p>
      {view.connect_error && <p className="banner banner-error">Connecting did not finish: {view.connect_error}</p>}
      {error && <p className="banner banner-error" role="alert">{error}</p>}
      {view.from_config && (
        <p className="banner banner-warn">
          argus.yml lists its own repos, so Updates shows those. The list below is what this
          connection can see.
        </p>
      )}

      {view.source === "app" && (
        <div className="panel">
          <h2 className="eyebrow">Connected</h2>
          <p>
            Through the GitHub App <strong><Ext href={view.app.html_url}>{view.app.name}</Ext></strong> on {view.app.owner}.
            {view.last_success && <> Last read {agoText(Date.now() / 1000 - view.last_success)}.</>}
          </p>
          {view.installations.length === 0 ? (
            <p className="st-failing">Not installed anywhere yet: <Ext href={view.install_url}>install it on your repos</Ext>.</p>
          ) : (
            <ul className="repo-list">
              {view.installations.map((i) => (
                <li key={i.id}>
                  {i.account}: {i.selection === "all" ? "all repos" : "chosen repos"}{" "}
                  · <Ext href={i.url}>change which repos</Ext>
                </li>
              ))}
            </ul>
          )}
          <div className="actions">
            <Ext href={view.install_url}>Install on another account</Ext>
            <button className="btn" onClick={() => load(true)}>Look for new repos</button>
          </div>
          {!confirm && <button className="link-button small" onClick={() => setConfirm(true)}>Disconnect…</button>}
          {confirm && (
            <p className="small">
              Argus forgets the app's key at once. Delete the app on GitHub as well, in{" "}
              <Ext href={view.settings_url}>its settings</Ext>.{" "}
              <button className="btn" onClick={disconnect}>Disconnect</button>{" "}
              <button className="link-button" onClick={() => setConfirm(false)}>Keep</button>
            </p>
          )}
        </div>
      )}
      {view.source === "file" && (
        <div className="panel">
          <h2 className="eyebrow">Connected</h2>
          <p>With the token file Argus was started with (ARGUS_GITHUB_TOKEN_FILE). Connecting an app here replaces it.</p>
        </div>
      )}

      {view.source !== "app" && <Connect session={session} setError={setError} />}
      {view.source !== "app" && <PasteToken session={session} view={view} onView={setView} setError={setError} />}
      {view.source !== "none" && <Deploys session={session} view={view} setError={setError} />}
      {view.source !== "none" && <Repos session={session} view={view} onView={setView} setError={setError} />}
    </section>
  );
}
