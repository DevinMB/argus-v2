import { useEffect, useRef, useState } from "react";
import { getCheck, send } from "../api.js";
import Track from "./Track.jsx";
import { ago, agoText, ms, siteName, stamp } from "../lib.js";

// Pausing: "I know it is down". The check keeps running so Argus sees it come
// back, but it shows grey and never pages.
function PauseForm({ check, csrf, onDone, onCancel }) {
  const [reason, setReason] = useState("");
  const [until, setUntil] = useState("up");
  const [error, setError] = useState(null);
  const save = async (e) => {
    e.preventDefault();
    try {
      onDone(await send("PUT", `/api/checks/${encodeURIComponent(check.name)}/pause`,
        { reason, until }, csrf));
    } catch (err) {
      setError(err.message);
    }
  };
  return (
    <form className="pause-form" onSubmit={save}>
      <label className="field"><span>Why (optional)</span>
        <input value={reason} maxLength={200} autoFocus onChange={(e) => setReason(e.target.value)}
               placeholder="Switched off until the GPU project is done" /></label>
      <label className="toggle"><input type="radio" checked={until === "up"} onChange={() => setUntil("up")} />
        <span><strong>Until it is back up</strong>
          <small>It resumes by itself once it has been down and answers again.</small></span></label>
      <label className="toggle"><input type="radio" checked={until === "resumed"} onChange={() => setUntil("resumed")} />
        <span><strong>Until I resume it</strong>
          <small>Stays paused even when it answers; press Resume when you are done.</small></span></label>
      {error && <p className="form-error">{error}</p>}
      <div className="actions">
        <button className="btn" type="submit">Pause</button>
        <button className="link-button" type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

// Detail for one check: its settings, last result, and a timeline of every
// change of state Argus recorded, newest first.
// Pausing needs only a session (csrf); changing or removing also needs an editable config (onEdit).
export default function CheckDrawer({ name, now, sites, csrf, onClose, onEdit, onRemove, onChanged }) {
  const [pausing, setPausing] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [check, setCheck] = useState(null);
  const [error, setError] = useState(null);
  const closeRef = useRef(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setCheck(null);
    setError(null);
    getCheck(name, ctrl.signal).then(setCheck).catch((e) => {
      if (e.name !== "AbortError") setError(e.message);
    });
    return () => ctrl.abort();
  }, [name]);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="scrim" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={name}
             onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <div>
            <div className="eyebrow">check</div>
            <h2>{name}</h2>
          </div>
          <button ref={closeRef} className="close" onClick={onClose} aria-label="Close">×</button>
        </header>
        {error && <p className="banner banner-error">{error}</p>}
        {!check && !error && <p className="muted">Loading…</p>}
        {check && (
          <>
            <div className={`drawer-status st-${check.status}`}>
              <span className={`dot dot-${check.status}`} />
              <span className="status-word">{check.status}</span>
              <span className="muted">for {ago(now - (check.paused ? check.paused.since : check.last_change))}</span>
            </div>
            {check.paused && (
              <p className="paused-note">
                {check.paused.reason ? <>“{check.paused.reason}”. </> : null}
                Argus still checks it (now {check.probe_status}) but it never pages
                {check.paused.until === "up" ? ", and it resumes by itself once it is back up." : " until you resume it."}
              </p>
            )}
            {check.last_error && <pre className="err">{check.last_error}</pre>}
            {csrf && check.paused && (
              <div className="actions">
                <button className="btn" onClick={async () => {
                  try {
                    setCheck(await send("DELETE", `/api/checks/${encodeURIComponent(name)}/pause`, undefined, csrf));
                    onChanged?.();
                  } catch (e) { setError(e.message); }
                }}>Resume</button>
                {check.probe_status === "down" && check.severity === "critical" && <span className="small">It is down now, so resuming sends you an alert.</span>}
              </div>
            )}
            {csrf && !check.paused && pausing && (
              <PauseForm check={check} csrf={csrf} onCancel={() => setPausing(false)}
                         onDone={(c) => { setPausing(false); setCheck(c); onChanged?.(); }} />
            )}
            {(onEdit || (csrf && !check.paused && !pausing)) && (
              <div className="actions">
                {csrf && !check.paused && !pausing && <button className="btn" onClick={() => setPausing(true)}>Pause…</button>}
                {onEdit && <button className="btn" onClick={() => onEdit(check)}>Change</button>}
                {onEdit && !confirm && <button className="btn" onClick={() => setConfirm(true)}>Remove…</button>}
                {onEdit && confirm && (
                  <>
                    <span className="small">Stop checking {name}? Its history goes too.</span>
                    <button className="btn" onClick={() => onRemove(check)}>Remove</button>
                    <button className="link-button" onClick={() => setConfirm(false)}>Keep</button>
                  </>
                )}
              </div>
            )}
            <dl className="facts">
              <dt>Target</dt><dd className="mono">{check.target}</dd>
              <dt>Kind</dt><dd>{check.kind}{check.kind === "http" && ` · expects ${check.expect_status.join("/")}${check.verify_tls ? "" : " · TLS not verified"}`}</dd>
              <dt>Checked from</dt><dd>{check.companion ? <a href="#/hosts">{check.companion}</a> : "Argus"}</dd>
              <dt>Site</dt><dd>{siteName(sites, check.site)}</dd>
              <dt>Group</dt><dd>{check.group}</dd>
              <dt>When down</dt><dd>{check.severity === "critical" ? "alerts you" : "dashboard only"}</dd>
              <dt>Every</dt><dd>{check.interval} s, timeout {check.timeout} s</dd>
              <dt>Last run</dt><dd>{agoText(now - check.last_run)} · {ms(check.last_duration)}</dd>
              <dt>Last success</dt><dd>{check.last_ok ? agoText(now - check.last_ok) : "never"}</dd>
            </dl>
            {/* Only a check an app's pipeline feeds has a track; the rest would say so in jargon. */}
            {check.track && <>
              <h3 className="eyebrow">Track{check.repo && <span className="count">{check.repo}</span>}</h3>
              <Track steps={check.track} now={now} />
            </>}
            <h3 className="eyebrow">History</h3>
            {check.transitions.length === 0 ? (
              <p className="muted">No changes recorded.</p>
            ) : (
              <ol className="timeline">
                {check.transitions.map((t, i) => (
                  <li key={i} className={`event to-${t.to_status}`}>
                    <span className="node" aria-hidden="true" />
                    <div className="note">
                      <div className="note-head">
                        <span className={`st-${t.from_status}`}>{t.from_status}</span>
                        <span className="arrow">→</span>
                        <span className={`st-${t.to_status}`}>{t.to_status}</span>
                        <time>{stamp(t.ts)}</time>
                      </div>
                      {t.error && <div className="note-body mono">{t.error}</div>}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </>
        )}
      </aside>
    </div>
  );
}
