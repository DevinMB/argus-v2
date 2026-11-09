import { useState } from "react";
import { send } from "../api.js";
import { safeHref } from "../lib.js";

// The app pipeline for one repo (decision 20): look over what the pull request
// adds, then open it. Argus never merges it.
const SAYS = {
  none: ["No CI pipeline", "Updates to it are not tested, scanned or merged on their own.", "Add the pipeline"],
  shared: ["Built by a shared workflow in another repo", "Argus's pipeline does the same work inside this repo, with the same image names.", "Switch to Argus's pipeline"],
  argus: ["Argus's pipeline", "Each update is built, tested and scanned; patch and minor updates merge on green.", null],
  own: ["Its own CI", "Argus follows its ci.yml runs and leaves the workflow alone.", null],
};

function Permission({ need }) {
  return (
    <div className="banner banner-warn pipe-perm">
      <p><strong>Argus&apos;s GitHub App may not write to repos yet.</strong> Once, on GitHub:</p>
      <ol className="steps-list">
        <li>
          {need.settings_url ? <a href={safeHref(need.settings_url)} target="_blank" rel="noreferrer">Open the app&apos;s permissions</a>
            : "Open the app's permissions"}, set <strong>Contents</strong>, <strong>Pull requests</strong> and{" "}
          <strong>Workflows</strong> to Read and write, and press Save changes.
        </li>
        <li>
          {need.install_url ? <a href={safeHref(need.install_url)} target="_blank" rel="noreferrer">Accept the new permissions</a>
            : "Accept the new permissions"} for your account. GitHub emails you the same link.
        </li>
        <li>Come back and press Open the pull request again. Each pull request uses a key for that one repo only, kept for nothing else.</li>
      </ol>
    </div>
  );
}

export default function Pipeline({ repo, csrf }) {
  const pipe = repo.pipeline ?? { kind: "none" };
  const [title, what, action] = SAYS[pipe.kind] ?? SAYS.own;
  const [plan, setPlan] = useState(null);
  const [edits, setEdits] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [need, setNeed] = useState(null);
  const [opened, setOpened] = useState(null);
  const path = `/api/github/pipeline/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.repo)}`;

  const look = async () => {
    setBusy(true); setError(null);
    try {
      const p = await send("GET", path, undefined, csrf);
      setPlan(p);
      setEdits(Object.fromEntries(p.images.map((i) => [i.job, { image: i.image, test_command: i.test_command }])));
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  };
  const open = async () => {
    setBusy(true); setError(null); setNeed(null);
    try {
      const images = Object.entries(edits).map(([job, e]) => ({ job, ...e }));
      setOpened((await send("POST", path, { images }, csrf)).url);
    } catch (e) {
      if (e.detail?.needs_permission) setNeed(e.detail);
      else setError(e.message);
    } finally { setBusy(false); }
  };
  const edit = (job, k, v) => setEdits((s) => ({ ...s, [job]: { ...s[job], [k]: v } }));

  return (
    <div className="pipe">
      <h4 className="eyebrow">pipeline</h4>
      <p>
        <strong>{title}{pipe.kind === "argus" && pipe.version ? `, version ${pipe.version}` : ""}.</strong>{" "}
        <span className="muted">{what}</span>
      </p>
      {opened ? (
        <p className="form-ok" role="status">
          Pull request opened: <a href={safeHref(opened)} target="_blank" rel="noreferrer">{opened.replace("https://github.com/", "")}</a>.
          Merge it on GitHub when it looks right; its first run builds and scans each image.
        </p>
      ) : action && csrf && !plan && (
        <button className="btn" type="button" disabled={busy} onClick={look}>{busy ? "Reading the repo…" : action}</button>
      )}
      {plan && !opened && (
        <div className="pipe-plan">
          {plan.blocked ? <p className="form-error">{plan.blocked}</p> : (
            <>
              <p className="muted small">
                A pull request adds {Object.keys(plan.files).map((f) => <code key={f}>{f}</code>).reduce((a, b) => [a, " and ", b])}.
                Nothing in it runs code from another repo.
              </p>
              <ul className="pipe-images">
                {plan.images.map((i) => (
                  <li key={i.job}>
                    <span className="mono small muted">{i.context === "." ? "" : `${i.context}/`}{i.dockerfile}</span>
                    <label className="field">
                      <span>Image</span>
                      <input value={edits[i.job]?.image ?? ""} spellCheck="false" onChange={(e) => edit(i.job, "image", e.target.value)} />
                    </label>
                    <label className="field">
                      <span>Test command, run inside the image</span>
                      <input value={edits[i.job]?.test_command ?? ""} placeholder="none: for example python -m pytest -q" spellCheck="false"
                             onChange={(e) => edit(i.job, "test_command", e.target.value)} />
                    </label>
                  </li>
                ))}
              </ul>
              {plan.notes.length > 0 && <ul className="pipe-notes small muted">{plan.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
              {Object.entries(plan.files).map(([f, text]) => (
                <details key={f} className="pipe-file">
                  <summary className="small">Show {f}</summary>
                  <pre className="log small">{text}</pre>
                </details>
              ))}
              {need && <Permission need={need} />}
              <div className="actions">
                <button className="btn btn-primary" type="button" disabled={busy} onClick={open}>
                  {busy ? "Opening…" : "Open the pull request"}
                </button>
                <button className="btn" type="button" disabled={busy} onClick={() => { setPlan(null); setNeed(null); }}>Cancel</button>
              </div>
              <p className="muted small">Argus never merges it: you do, on GitHub.</p>
            </>
          )}
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}
