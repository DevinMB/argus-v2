import { ago, agoText, isPublic, siteName } from "../lib.js";

const LABEL = { up: "up", failing: "failing", down: "down", unknown: "waiting", paused: "paused" };
const RANK = { down: 0, failing: 1, unknown: 2, paused: 3, up: 4 };

// Every check on one table, problems first: what it probes, whether it alerts
// you, and how it is doing. The drawer has the history.
export default function CheckGrid({ checks, now, sites, onPick }) {
  if (!checks.length) return <p className="empty">No checks match.</p>;
  const many = Object.keys(sites ?? {}).length > 1;
  const rows = [...checks].sort((a, b) => (RANK[a.status] - RANK[b.status]) || a.group.localeCompare(b.group)
    || a.name.localeCompare(b.name));
  return (
    <div className="ctable" role="table">
      <div className="ctable-row ctable-head" role="row">
        <span className="eyebrow">check</span><span className="eyebrow">what it probes</span>
        <span className="eyebrow">when down</span><span className="eyebrow">now</span>
      </div>
      {rows.map((c) => (
        <button key={c.name} className={`ctable-row ctable-${c.status}`} role="row" onClick={() => onPick(c.name)}
                title={c.description || undefined}>
          <span className="ct-name">
            <span className={`dot dot-${c.status}`} aria-hidden="true" />
            <span className="ct-title"><strong>{c.name}</strong>
              <span className="muted small">{c.group}{many ? ` · ${siteName(sites, c.site)}` : ""}</span></span>
          </span>
          <span className="ct-target"><span className="kind">{c.kind.toUpperCase()}</span>
            <span className="mono" title={c.target}>{c.target.replace(/^https?:\/\//, "").replace(/\/$/, "")}</span>
            {isPublic(c) && <span className="chip chip-public" title="Reached from the internet">public</span>}</span>
          <span className={`ct-alerts small ${c.severity === "critical" ? "st-accent" : "muted"}`}>
            {c.severity === "critical" ? "alerts you" : "shows here"}</span>
          <span className="ct-now small">
            <span className={`st-${c.status}`}>{LABEL[c.status] ?? c.status}</span>
            <span className="muted"> {c.paused ? agoText(now - c.paused.since)
              : c.last_change ? `for ${ago(now - c.last_change)}` : ""}</span>
          </span>
          {c.status !== "up" && (c.paused?.reason || c.last_error) && (
            <span className="ct-why small">{c.paused ? (c.paused.reason || "paused") : c.last_error}</span>
          )}
        </button>
      ))}
    </div>
  );
}
