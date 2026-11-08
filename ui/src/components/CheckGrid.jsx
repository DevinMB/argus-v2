import { ago, agoText, isPublic, placeOf, siteName } from "../lib.js";

const LABEL = { up: "up", failing: "failing", down: "down", unknown: "waiting", paused: "paused" };
const RANK = { down: 0, failing: 1, unknown: 2, paused: 3, up: 4 };

// Every check on one table, in its group, problems first: what it probes,
// whether it alerts you, and how it is doing. The drawer has the history.
function Row({ c, now, many, sites, onPick }) {
  return (
    <button className={`ctable-row ctable-${c.status}`} role="row" onClick={() => onPick(c.name)}
            title={c.description || undefined}>
      <span className="ct-name">
        <span className={`dot dot-${c.status}`} aria-hidden="true" />
        <span className="ct-title"><strong>{c.name}</strong>
          {many && <span className="muted small">{siteName(sites, c.site)}</span>}</span>
      </span>
      <span className="ct-target"><span className="kind">{c.kind.toUpperCase()}</span>
        <span className="mono" title={c.target}>{c.target.replace(/^https?:\/\//, "").replace(/\/$/, "")}</span>
        {isPublic(c) && <span className="chip chip-public" title="Reached from the internet">public</span>}</span>
      <span className={`ct-alerts small ${c.severity === "critical" ? "st-accent" : "muted"}`}>
        {c.severity === "critical" ? "alerts you" : "dashboard only"}</span>
      <span className="ct-now small">
        <span className={`st-${c.status}`}>{LABEL[c.status] ?? c.status}</span>
        <span className="muted"> {c.paused ? agoText(now - c.paused.since)
          : c.last_change ? `for ${ago(now - c.last_change)}` : ""}</span>
      </span>
      {c.status !== "up" && (c.paused?.reason || c.last_error) && (
        <span className="ct-why small">{c.paused ? (c.paused.reason || "paused") : c.last_error}</span>
      )}
    </button>
  );
}

// By host: public names first, then each host, then devices with no companion.
const PLACE = (g) => (g === "Websites" ? 0 : g === "Other devices" ? 2 : 1);

export default function CheckGrid({ checks, now, sites, groupNames, onPick, groupBy = "group", hosts = [] }) {
  if (!checks.length) return <p className="empty">No checks match.</p>;
  const many = Object.keys(sites ?? {}).length > 1;
  const groups = {};
  const keyOf = (c) => (groupBy === "host" ? placeOf(c, hosts) : c.group);
  for (const c of checks) (groups[keyOf(c)] ??= []).push(c);
  const worst = (list) => Math.min(...list.map((c) => RANK[c.status] ?? 2));
  const order = Object.keys(groups).sort((a, b) => (worst(groups[a]) - worst(groups[b]))
    || (groupBy === "host" ? PLACE(a) - PLACE(b) : 0) || a.localeCompare(b));
  return (
    <div className="ctable" role="table">
      <div className="ctable-row ctable-head" role="row">
        <span className="eyebrow">check</span><span className="eyebrow">what it probes</span>
        <span className="eyebrow">when down</span><span className="eyebrow">now</span>
      </div>
      {order.map((g) => {
        const list = groups[g].sort((a, b) => (RANK[a.status] - RANK[b.status]) || a.name.localeCompare(b.name));
        const bad = list.filter((c) => c.status !== "up" && c.status !== "paused").length;
        return (
          <div key={g} role="rowgroup" className="ctable-group">
            <div className="ctable-ghead" role="row">
              <span className="ct-gname">{groupBy === "host" ? g : (groupNames?.[g] ?? g)}</span>
              <span className={`small ${bad ? "st-failing" : "muted"}`}>
                {bad ? `${bad} of ${list.length} need a look` : `${list.length} up`}</span>
            </div>
            {list.map((c) => <Row key={c.name} c={c} now={now} many={many} sites={sites} onPick={onPick} />)}
          </div>
        );
      })}
    </div>
  );
}
