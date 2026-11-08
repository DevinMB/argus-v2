import { useState } from "react";
import { send } from "../api.js";

// Ports checked twice (say, an imported HTTP check and a suggested TCP one):
// one notice, the check each port keeps, and one button to remove the rest.
export default function Duplicates({ sets, session, onDone }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  if (!sets.length) return null;
  const extra = sets.flatMap((s) => s.slice(1));
  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      for (const c of extra) {
        const { base } = await send("GET", "/api/config"); // each removal changes the file
        await send("DELETE", `/api/config/entries/checks/${encodeURIComponent(c.name)}?base=${base}`, undefined, session.csrf);
      }
      onDone(`${extra.length} extra check${extra.length === 1 ? "" : "s"} removed.`);
    } catch (e) {
      setError(e.message);
      onDone(null);
    }
    setBusy(false);
  };
  return (
    <section className="panel dupes" aria-label="Ports checked twice">
      <p>
        <strong>{sets.length} port{sets.length === 1 ? " is" : "s are"} checked twice.</strong>{" "}
        <span className="muted">Each extra check runs and alerts on its own, so one outage shows twice.</span>{" "}
        <button className="link-button" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Hide" : "Review"}</button>
      </p>
      {open && (
        <>
          <ul className="rows">{sets.map(([keep, ...rest]) => (
            <li key={keep.name}>
              <span className="row-main"><strong>Keep {keep.name}</strong>
                <span className="row-sub muted"><span className="kind">{keep.kind.toUpperCase()}</span> <span className="mono">{keep.target}</span></span></span>
              <span className="row-note small muted">remove {rest.map((c) => `${c.name} (${c.kind.toUpperCase()})`).join(", ")}</span>
            </li>
          ))}</ul>
          <p className="muted small">Argus keeps the check that alerts you, then the HTTP one (it proves the app answers, not only the port).</p>
          <div className="actions">
            <button className="btn btn-primary" disabled={busy} onClick={remove}>
              {busy ? "Removing…" : `Remove ${extra.length} extra check${extra.length === 1 ? "" : "s"}`}</button>
          </div>
        </>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </section>
  );
}
