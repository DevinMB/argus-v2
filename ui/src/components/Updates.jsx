import { useEffect, useState } from "react";
import Pipeline from "./Pipeline.jsx";
import Track from "./Track.jsx";
import {
  STAGES, isAuto, agoText, lifecycle, lifecycleHeadline, recentMerges, safeHref, tidyTitle,
} from "../lib.js";

const CI = { success: "passed", failure: "failed", running: "running", none: "no CI yet" };
const GROUPS = [
  ["you", "Needs you", "A person has to act before these move again."],
  ["moving", "On the way", "The pipeline is working on these; nothing to do."],
  ["done", "Up to date", "Running their newest change."],
  ["untracked", "Not tracked", "No CI runs on the default branch, so there is nothing to follow. Open one to add Argus's pipeline."],
];

function Link({ href, className, children }) {
  const safe = safeHref(href);
  return safe
    ? <a className={className} href={safe} target="_blank" rel="noreferrer">{children}</a>
    : <span className={className}>{children}</span>;
}

const untracked = (s) => s.label === "Not tracked";
const groupOf = (s) => (s.you ? "you" : s.tone === "accent" ? "moving" : untracked(s) ? "untracked" : "done");

// Where every app is right now: one column per stage a change passes through.
function Board({ rows }) {
  return (
    <ol className="board" aria-label="Where each app is">
      {STAGES.map(([key, title, hint], i) => {
        const here = rows.filter((r) => r.state.column === key && !untracked(r.state));
        return (
          <li key={key} className={`lane ${here.length ? "lane-busy" : ""}`}>
            <div className="lane-head">
              <span className="lane-n">{i + 1}</span>
              <span className="lane-title">{title}</span>
              <span className="lane-count">{here.length}</span>
            </div>
            <p className="lane-hint">{hint}</p>
            <div className="lane-apps">
              {!here.length && <span className="lane-empty">none</span>}
              {key === "live" && here.length > 0 && <a className="lane-empty" href="#up-to-date">listed below</a>}
              {key !== "live" && here.map(({ repo, state }) => (
                <a key={repo.name} href={`#/updates/${encodeURIComponent(repo.name)}`}
                   className={`app-chip tone-bg-${state.tone}`} title={state.label}>
                  {repo.name}
                </a>
              ))}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function Details({ repo, now, csrf }) {
  const byGate = repo.merged.filter((m) => isAuto(m.by)).length;
  return (
    <div className="app-more">
      <Track steps={repo.track} now={now} />
      {repo.updates.length > 0 && (
        <>
          <h4 className="eyebrow">open update PRs</h4>
          <ul className="prs">
            {repo.updates.map((u) => (
              <li key={u.number}>
                <Link href={u.url}>{u.title}</Link>
                {u.waits_for_you
                  ? <span className="chip chip-critical">waits for you</span>
                  : <span className={`chip ci-${u.ci}`}>CI {CI[u.ci] ?? u.ci}</span>}
              </li>
            ))}
          </ul>
        </>
      )}
      {repo.alerts?.length > 0 && (
        <>
          <h4 className="eyebrow">security alerts</h4>
          <ul className="prs alerts">
            {repo.alerts.map((a) => (
              <li key={a.url}>
                <Link href={a.url}>{a.package}: {a.summary}</Link>
                <span className={`chip sev-${a.severity}`}>{a.severity}</span>
                <span className="muted">open {agoText(now - a.created_at)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {repo.owner && <Pipeline repo={repo} csrf={csrf} />}
      <p className="app-facts">
        <Link href={repo.url}>{repo.name} on GitHub</Link>
        {repo.main && <span className="mono">sha-{repo.main.sha}</span>}
        <span>{repo.merged.length} merged in 14 days{repo.merged.length ? `, ${byGate} by the gate` : ""}</span>
        <span>{repo.alerts == null ? "alerts unreadable" : repo.alerts.length ? "" : "no open alerts"}</span>
        {repo.check && <a href={`#/check/${encodeURIComponent(repo.check)}`}>health check: {repo.check}</a>}
      </p>
    </div>
  );
}

const lastChange = (repo) => {
  const t = tidyTitle(repo.main?.title) || `sha-${repo.main?.sha}`;
  return t.charAt(0).toUpperCase() + t.slice(1);
};

// One app as a row: its state in words, when it last changed, and the full
// track when opened.
function AppRow({ repo, state, now, open, onToggle, grouped, csrf }) {
  // When it went live if we know, else when its newest change merged.
  const when = state.deployedAt ? `deployed ${agoText(now - state.deployedAt)}`
    : repo.main?.started_at ? `merged ${agoText(now - repo.main.started_at)}` : "";
  const alerts = repo.alerts?.length ?? 0;
  return (
    <li id={`app-${repo.name}`} className={`app app-${state.tone}`}>
      <button className="app-row" aria-expanded={open} onClick={onToggle}>
        <span className={`dot dot-${state.tone}`} aria-hidden="true" />
        <span className="app-name">{repo.name}</span>
        <span className={`app-label ${grouped ? "app-where" : `st-${state.tone}`}`}>{grouped ? state.where : state.label}</span>
        {/* Up to date, the newest change says it all; how it ships sits in the label column. */}
        <span className="app-sentence">{grouped ? lastChange(repo) : state.sentence}</span>
        <span className="app-when">
          {alerts > 0 && <span className="chip sev-high">{alerts} alert{alerts > 1 ? "s" : ""}</span>}
          {when}
        </span>
      </button>
      {open && <Details repo={repo} now={now} csrf={csrf} />}
    </li>
  );
}

// What the pipeline did lately, newest first.
function Recent({ repos, now }) {
  const merges = recentMerges(repos);
  if (!merges.length) return null;
  return (
    <section className="group">
      <h2 className="eyebrow">recently merged</h2>
      <ul className="recent">
        {merges.map((m) => (
          <li key={`${m.repo}-${m.number}`}>
            <span className="recent-repo">{m.repo}</span>
            <Link href={m.url}>{m.title}</Link>
            <span className="muted">{isAuto(m.by) ? "automatically" : m.by ? `by ${m.by}` : ""}</span>
            <span className="muted recent-when">{agoText(now - m.merged_at)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// Per app: where its newest change is on the way to production, in words.
// Data is polled from GitHub by Argus. #/updates/<repo> opens one app.
export default function Updates({ updates, now, focus, csrf }) {
  const [opened, setOpened] = useState(() => new Set(focus ? [focus] : []));
  const [lastFocus, setLastFocus] = useState(focus);
  if (focus !== lastFocus) {
    setLastFocus(focus);
    if (focus) setOpened((s) => new Set(s).add(focus));
  }
  useEffect(() => {
    if (focus) document.getElementById(`app-${focus}`)?.scrollIntoView({ block: "center" });
  }, [focus, updates?.last_success]);
  if (!updates?.enabled) {
    return (
      <section className="updates-head">
        <h1>See every update on its way.</h1>
        <p className="lede">
          Connect GitHub and this page follows each repo's changes: Dependabot's update PRs, CI,
          the merge, the build and its scan. Argus only reads, and nothing needs to reach it from
          the internet.
        </p>
        <div className="actions"><a className="btn btn-primary" href="#/github">Connect GitHub</a></div>
      </section>
    );
  }
  if (updates.last_success && !updates.repos.length) {
    return <p className="empty">Connected, with no repos to show yet. Choose some on the <a href="#/github">GitHub page</a>.</p>;
  }
  if (!updates.last_success) {
    return <p className="muted pad">{updates.last_error ? `GitHub poll failed: ${updates.last_error}` : "Polling GitHub…"}</p>;
  }
  const rows = updates.repos.map((repo) => ({ repo, state: lifecycle(repo) }));
  const states = rows.map((r) => r.state);
  const toggle = (name) => setOpened((s) => {
    const n = new Set(s);
    if (n.has(name)) n.delete(name); else n.add(name);
    return n;
  });
  return (
    <>
      <section className="updates-head">
        <h1>{lifecycleHeadline(states)}</h1>
        <p className="lede">
          Dependabot opens a PR for each new version. Each update is tested, merged, built and
          scanned on its own; only major versions wait for you. GitHub read{" "}
          {agoText(now - updates.last_success)} · <a href="#/github">GitHub settings</a>.
        </p>
      </section>
      {updates.last_error && (
        <p className="banner banner-warn">
          The last GitHub poll failed ({updates.last_error}); showing the one from {agoText(now - updates.last_success)}.
        </p>
      )}

      <Board rows={rows} />

      {GROUPS.map(([key, title, hint]) => {
        const list = rows.filter((r) => groupOf(r.state) === key);
        if (!list.length) return null;
        return (
          <section key={key} id={key === "done" ? "up-to-date" : undefined} className={`group group-${key}`}>
            <h2 className="eyebrow">{title} <span className="count">{list.length}</span></h2>
            <p className="group-hint">{hint}</p>
            <ul className="apps">
              {list.map(({ repo, state }) => (
                <AppRow key={repo.name} repo={repo} state={state} now={now} grouped={key === "done" && state.label === "Up to date"}
                        open={opened.has(repo.name)} onToggle={() => toggle(repo.name)} csrf={csrf} />
              ))}
            </ul>
          </section>
        );
      })}

      <Recent repos={updates.repos} now={now} />
    </>
  );
}
