import { useCallback, useEffect, useMemo, useState } from "react";
import { useArgus, useSession } from "./api.js";
import Account from "./components/Account.jsx";
import AlertsPage from "./components/AlertsPage.jsx";
import GitHubPage from "./components/GitHubPage.jsx";
import ConfigPage, { CheckForm, savedText } from "./components/ConfigPage.jsx";
import { send } from "./api.js";
import SignIn from "./components/SignIn.jsx";
import CheckDrawer from "./components/CheckDrawer.jsx";
import CheckGrid from "./components/CheckGrid.jsx";
import Asides from "./components/Asides.jsx";
import GetStarted, { coverageLine, SetupBar } from "./components/GetStarted.jsx";
import Heartbeats from "./components/Heartbeats.jsx";
import DeploySetup from "./components/DeploySetup.jsx";
import Hosts from "./components/Hosts.jsx";
import Jobs from "./components/Jobs.jsx";
import Meter from "./components/Meter.jsx";
import Ring from "./components/Ring.jsx";
import Updates from "./components/Updates.jsx";
import { acrossSites, ago, agoText, byStatus, filterChecks, groupChecks, headline, joinList, lifecycle, siteName, siteNote, summarize } from "./lib.js";

const STATUS_FILTERS = [["all", "All"], ["problems", "Problems"], ["up", "Up"], ["paused", "Paused"]];

// The view and the selected check live in the hash (#/updates, #/updates/<repo>,
// #/hosts, #/alerts, #/account, #/github, #/config, #/check/<name>) so a link can open any of them. A
// check's drawer opens over the Checks view.
function useView() {
  const read = () => {
    const m = window.location.hash.match(/^#\/updates(?:\/(.+))?$/);
    if (m) return ["updates", m[1] && decodeURIComponent(m[1])];
    // #/github may carry GitHub's ?installation_id=... after an install.
    const page = window.location.hash.match(/^#\/(hosts|alerts|account|github|config|deploy)(?:\?.*)?$/);
    return page ? [page[1], null] : ["checks", null];
  };
  const [view, setView] = useState(read);
  useEffect(() => {
    const on = () => setView(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return view;
}

function useSelected() {
  const read = () => {
    const m = window.location.hash.match(/^#\/check\/(.+)$/);
    return m ? decodeURIComponent(m[1]) : null;
  };
  const [name, setName] = useState(read);
  useEffect(() => {
    const on = () => setName(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const select = useCallback((n) => {
    window.location.hash = n ? `/check/${encodeURIComponent(n)}` : "";
  }, []);
  return [name, select];
}

function Segmented({ value, options, onChange, label }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map(([v, text]) => (
        <button key={v} aria-pressed={value === v} onClick={() => onChange(v)}>{text}</button>
      ))}
    </div>
  );
}

// Footer links come from argus.yml, grouped and in config order.
function linkGroups(links = []) {
  const out = {};
  for (const l of links) (out[l.group] ??= []).push(l);
  return out;
}

export default function App() {
  const { data, error, fetchedAt, reload } = useArgus();
  const [session, setSession] = useSession();
  const [selected, select] = useSelected();
  const [view, focus] = useView();
  const [status, setStatus] = useState("all");
  const [site, setSite] = useState("all");
  const [q, setQ] = useState("");
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const checks = data?.checks ?? [];
  const sites = data?.info.sites ?? {};
  const siteOrder = Object.keys(sites).length ? Object.keys(sites) : [...new Set(checks.map((c) => c.site))];
  const manySites = siteOrder.length > 1;
  const siteFilters = [["all", "All sites"], ...siteOrder.map((s) => [s, siteName(sites, s)])];
  // Server time, advanced locally between refreshes, so ages use Argus's clock.
  const now = data ? data.info.now + (Date.now() - fetchedAt) / 1000 : Date.now() / 1000;
  const sum = useMemo(() => summarize(checks), [checks]);
  const problems = useMemo(
    () => checks.filter((c) => c.status === "down" || c.status === "failing").sort(byStatus),
    [checks],
  );
  const shown = useMemo(() => filterChecks(checks, { status, site, q, sites }), [checks, status, site, q, sites]);
  const siteSum = (s) => summarize(checks.filter((c) => c.site === s));
  // A paused check pages nobody, so it is not among them.
  const critical = checks.filter((c) => c.severity === "critical" && c.status !== "paused");
  const pausedCritical = checks.filter((c) => c.severity === "critical" && c.status === "paused").length;
  const stalled = data && now - data.info.last_tick > 30;
  const needYou = (data?.updates?.repos ?? []).filter((r) => lifecycle(r).you).length;
  const alertsUnset = data?.info.alerts && !data.info.alerts.ready;
  const quietHosts = (data?.companions?.companions ?? []).filter((h) => h.status === "quiet").length;
  const locked = session?.view_requires_login && !session.signed_in;
  const settingsView = ["alerts", "account", "github", "config"].includes(view);
  // Editing checks: the form, and whether this config can be edited here at all.
  const [editing, setEditing] = useState(null); // null, "new", {initial} or a check
  const [editable, setEditable] = useState(false);
  const [saved, setSaved] = useState(null);
  useEffect(() => {
    if (!session?.signed_in) return setEditable(false);
    send("GET", "/api/config").then((c) => setEditable(c.editable), () => setEditable(false));
  }, [session?.signed_in, view]);
  const afterSave = (out) => {
    setEditing(null);
    setSaved(savedText(out));
    reload();
  };
  const removeCheck = async (check) => {
    try {
      const c = await send("GET", "/api/config");
      const out = await send("DELETE", `/api/config/entries/checks/${encodeURIComponent(check.name)}?base=${c.base}`,
        undefined, session.csrf);
      select(null);
      afterSave(out);
    } catch (e) {
      setSaved(`Not removed: ${e.message}`);
    }
  };

  return (
    <div className="page">
      <header className="top">
        <a className="brand" href="#">
          <img src="/favicon.svg" alt="" width="22" height="22" />
          <span>ARGUS</span>
        </a>
        <nav className="tabs" aria-label="View">
          <a href="#" aria-current={view === "checks" ? "page" : undefined}>Checks</a>
          <a href="#/updates" aria-current={view === "updates" ? "page" : undefined}>
            Updates{needYou > 0 && <span className="tab-badge">{needYou}</span>}
          </a>
          <a href="#/hosts" aria-current={view === "hosts" ? "page" : undefined}>
            Hosts{quietHosts > 0 && <span className="tab-badge" title="Hosts that stopped reporting">{quietHosts}</span>}
          </a>
          <a href="#/alerts" aria-current={view === "alerts" ? "page" : undefined}>
            Alerts{alertsUnset && <span className="tab-badge" title="Nothing sends alerts yet">!</span>}
          </a>
        </nav>
        <div className="top-meta">
          <span className={`live ${error || stalled ? "live-off" : ""}`}>
            {error ? "API unreachable" : stalled ? "scheduler stalled" : "live"}
          </span>
          {fetchedAt && <span className="hide-sm">updated {agoText((Date.now() - fetchedAt) / 1000)}</span>}
          {data && <span className="hide-sm hide-md">config {data.info.config}</span>}
          {data && <span className="hide-sm hide-md">build {data.info.commit.slice(0, 7)}</span>}
          {session && (session.signed_in
            ? <a className="top-link" href="#/account" aria-current={["account", "github", "config"].includes(view) ? "page" : undefined}>Settings</a>
            : <a className="top-link" href="#/account">{session.setup_needed ? "Set up" : "Sign in"}</a>)}
        </div>
      </header>

      {error && data && <p className="banner banner-error">Showing the last good data: {error}</p>}
      {session?.signed_in && data?.start && !data.start.exported_at && data.start.done >= 2 && (
        <p className="banner banner-warn">
          Argus's own settings are not backed up yet: its secrets and hosts live only in its data volume.{" "}
          <a href="#/account">Export them</a>.
        </p>
      )}
      {data?.info.config_reload_ok === false && (
        <p className="banner banner-warn">
          The newest argus.yml was rejected; the previous config is still running.
        </p>
      )}

      <SetupBar start={data?.start} session={session} view={view} />

      {(locked || (settingsView && session && !session.signed_in)) && (
        <SignIn session={session} onSession={setSession}
                why={locked ? "This Argus asks for the admin password to view anything." : undefined} />
      )}
      {!locked && session?.signed_in && ["account", "github", "config"].includes(view) && (
        <nav className="subtabs" aria-label="Settings">
          <a href="#/account" aria-current={view === "account" ? "page" : undefined}>Account</a>
          <a href="#/github" aria-current={view === "github" ? "page" : undefined}>GitHub</a>
          <a href="#/config" aria-current={view === "config" ? "page" : undefined}>Config</a>
        </nav>
      )}
      {!locked && settingsView && session?.signed_in && view === "alerts" && <AlertsPage session={session} />}
      {!locked && settingsView && session?.signed_in && view === "github" && <GitHubPage session={session} />}
      {!locked && session?.signed_in && view === "deploy" && <DeploySetup session={session} data={data} reload={reload} />}
      {!locked && settingsView && session?.signed_in && view === "config" && <ConfigPage session={session} />}
      {!locked && settingsView && session?.signed_in && view === "account" && (
        <Account session={session} onSession={setSession} />
      )}

      {!locked && !settingsView && !data && !error && <p className="muted pad">Loading…</p>}
      {!locked && !settingsView && !data && error && <p className="banner banner-error">Cannot reach the Argus API: {error}</p>}

      {!locked && data && view === "hosts" && <Hosts data={data.companions} session={session} reload={reload} start={data.start}
                 refs={[...data.jobs.map((j) => `job:${j.name}`), ...data.heartbeats.map((h) => `heartbeat:${h.name}`)]} />}

      {!locked && data && view === "updates" && <Updates updates={data.updates} now={now} focus={focus} />}

      {!locked && data && view === "checks" && (
        <>
          {session?.signed_in && editable && (
            <GetStarted start={data.start} session={session} reload={reload}
                        onWatch={(c) => { setSaved(null); setEditing({ initial: c }); }}
                        onAddCheck={() => { setSaved(null); setEditing("new"); }} />
          )}
          <section className="hero">
            <div className="eye">
              <Ring checks={checks} siteOrder={siteOrder} onPick={select} />
              <div className="eye-center">
                {sum.down ? (
                  <><div className="big st-down">{sum.down}</div><div className="eyebrow">down</div></>
                ) : (
                  <><div className="big">{sum.up}<span className="of">/{sum.total}</span></div>
                    <div className="eyebrow">checks up</div></>
                )}
              </div>
            </div>
            <div className="hero-side">
              <h1>{headline(sum)}</h1>
              {checks.length ? (
                <p className="lede">
                  {checks.length} check{checks.length === 1 ? "" : "s"} every minute{acrossSites(siteOrder.length)}, covering{" "}
                  {joinList(groupChecks(checks).map(([g]) => data.info.groups?.[g] ?? g))}. Three
                  failures in a row and a check is down; the critical ones page you.
                </p>
              ) : (
                <p className="lede">
                  Nothing to check yet. {editable
                    ? <>Press <strong>Add a check</strong> below for anything with a URL, a port or an address.</>
                    : <>Sign in and press <strong>Add a check</strong>, or edit <code>argus.yml</code>.</>}{" "}
                  Meanwhile, <a href="#/alerts">set up alerts</a> so Argus can reach you.
                </p>
              )}
              {coverageLine(data.start?.coverage) && <p className="coverage small">{coverageLine(data.start.coverage)}{data.start.suggestions.length > 0 && <> · <a href="#/hosts">{data.start.suggestions.length} to watch</a></>}</p>}
              <Asides checks={checks} heartbeats={data.heartbeats} alerts={data.info.alerts} />
              <div className="meters">
                {manySites && siteOrder.map((s) => {
                  const ss = siteSum(s);
                  return <Meter key={s} label={siteName(sites, s)} value={ss.up} total={ss.total}
                                tone={ss.down ? "down" : ss.failing ? "failing" : "up"}
                                sub={siteNote(ss)} />;
                })}
                <Meter label="Pages if down" value={critical.filter((c) => c.status === "up").length}
                       total={critical.length} tone="accent"
                       sub={`critical checks up${pausedCritical ? `, ${pausedCritical} paused` : ""}`} />
              </div>
            </div>
          </section>

          <section className="attention" aria-live="polite">
            {problems.length === 0 ? (
              <p className="quiet">Nothing needs attention.</p>
            ) : (
              <ul>
                {problems.map((c) => (
                  <li key={c.name}>
                    <button className="attn" onClick={() => select(c.name)}>
                      <span className={`dot dot-${c.status}`} />
                      <strong>{c.name}</strong>
                      <span className={`st-${c.status}`}>{c.status}</span>
                      <span className="muted">{c.last_error}</span>
                      <span className="muted">{ago(now - c.last_change)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="group">
            <h2 className="eyebrow">scheduled runs <span className="count">{data.jobs.length}</span></h2>
            <p className="group-hint">
              {data.info.jobs_intro || "Runs that start on their own and report back here when they finish. A failed or overdue run pages."}
            </p>
            <Jobs jobs={data.jobs} now={now} sites={sites} />
          </section>

          {saved && <p className="form-ok" role="status">{saved}</p>}
          {editing && (
            <CheckForm session={session} check={editing === "new" || editing.initial ? null : editing}
                       initial={editing.initial} sites={sites}
                       groups={groupChecks(checks).map(([g]) => g)}
                       hosts={(data.companions?.companions ?? []).map((h) => h.name)}
                       onDone={afterSave} onCancel={() => setEditing(null)} />
          )}

          <section className="toolbar">
            <Segmented label="Status" value={status} options={STATUS_FILTERS} onChange={setStatus} />
            {manySites && <Segmented label="Site" value={site} options={siteFilters} onChange={setSite} />}
            <input className="search" type="search" placeholder="Filter by name, target or tag"
                   value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter checks" />
            {editable && !editing && <button className="btn" onClick={() => { setSaved(null); setEditing("new"); }}>Add a check</button>}
          </section>

          <CheckGrid checks={shown} now={now} sites={sites} groups={data.info.groups} onPick={select} />

          <section className="group">
            <h2 className="eyebrow">timers <span className="count">{data.heartbeats.length}</span></h2>
            <p className="group-hint">Background jobs that report in after each run; one that goes quiet is late.</p>
            <Heartbeats heartbeats={data.heartbeats} now={now} sites={sites} />
          </section>
        </>
      )}

      <footer className="foot">
        <div>
          <div className="eyebrow">Argus</div>
          <a href="/api/checks">/api/checks</a>
          <a href="/metrics">/metrics</a>
        </div>
        {Object.entries(linkGroups(data?.info.links)).map(([group, links]) => (
          <div key={group}>
            <div className="eyebrow">{group}</div>
            {links.map((l) => <a key={l.url} href={l.url}>{l.label}</a>)}
          </div>
        ))}
        <div className="foot-note">Checks live in argus.yml; alerts and access are set here, behind the admin password.</div>
      </footer>

      {selected && <CheckDrawer name={selected} now={now} sites={sites} csrf={session?.signed_in ? session.csrf : undefined}
                                onChanged={reload} onClose={() => select(null)}
                                onEdit={editable ? (c) => { select(null); setSaved(null); setEditing(c); } : undefined}
                                onRemove={removeCheck} />}
    </div>
  );
}
