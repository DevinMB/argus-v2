import { useEffect, useState } from "react";
import { send } from "../api.js";
import Adopt from "./Adopt.jsx";
import { advisoryUrl, agoText, attention, backupLabel, bytes, containerTrouble, diskTone, guestHostName, hostSentence, hostSub, hostTree, guestsNet, installHelp, patchLabel, sortContainers } from "../lib.js";

const STATUS = { ok: ["up", "reporting"], quiet: ["down", "gone quiet"], waiting: ["unknown", "waiting"] };
const FIRST_CONTAINERS = 8;

export function Copy({ text }) {
  const [done, setDone] = useState(false);
  return (
    <button className="btn" type="button" onClick={() => navigator.clipboard?.writeText(text).then(
      () => { setDone(true); setTimeout(() => setDone(false), 2000); }, () => {})}>
      {done ? "Copied" : "Copy"}
    </button>
  );
}

// "Add a host": one command with a code for one host or several, then wait for them to appear.
function AddHost({ session, hosts, tlsPin, onClose, initialName = "" }) {
  const [url, setUrl] = useState(window.location.origin);
  const [name, setName] = useState(initialName);
  const [readOnly, setReadOnly] = useState(false);
  const [count, setCount] = useState(1);
  const [made, setMade] = useState(null);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);

  const make = async (e) => {
    e.preventDefault();
    setError(null);
    try {
      const several = count > 1;
      const out = await send("POST", "/api/companions/codes",
        { url, name: several ? "" : name, read_only: readOnly, hosts: count }, session.csrf);
      setMade({ ...out, count, before: new Set(hosts.map((h) => h.id)) });
    } catch (err) {
      setError(err.message);
    }
  };

  const arrived = made ? hosts.filter((h) => !made.before.has(h.id)) : [];
  const joined = made && arrived.length >= made.count && arrived[arrived.length - 1];
  const left = made ? Math.max(0, Math.round(made.expires - now)) : 0;
  const plain = url.startsWith("http://");

  return (
    <form className="panel" onSubmit={make}>
      <h2 className="eyebrow">Add a host</h2>
      {!made && (
        <>
          <p className="muted">
            Run one command on the host. It installs the companion, a small program that reports
            the host's containers, disks and updates to Argus once a minute. It only ever calls
            Argus; nothing on the host listens for connections.
          </p>
          <label className="field">
            <span>Argus's address, as the host reaches it</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} required spellCheck="false" />
            {plain && (
              <small className="st-failing">
                Plain HTTP: the host's token would cross the network unencrypted. Use Argus's
                https:// address unless a proxy in front of Argus does HTTPS.
              </small>
            )}
            {!plain && !tlsPin && (
              <small>Argus is behind a proxy, so the host checks the proxy's certificate as usual.</small>
            )}
          </label>
          <label className="field">
            <span>How many hosts will run it</span>
            <input type="number" min="1" max="50" value={count}
                   onChange={(e) => setCount(Math.max(1, Math.min(50, Number(e.target.value) || 1)))} />
            <small>Adding several guests? Make one command and run it on each, within an hour; each host takes its own name.</small>
          </label>
          {count === 1 && <label className="field">
            <span>Name (optional)</span>
            <input value={name} onChange={(e) => setName(e.target.value.toLowerCase())} placeholder="the host's own name"
                   pattern="[a-z0-9][a-z0-9\-]{0,62}" spellCheck="false" />
            <small>Lowercase letters, digits and dashes. Checks in argus.yml use it in <code>companion:</code>.</small>
          </label>}
          <label className="toggle">
            <input type="checkbox" checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)} />
            <span>
              <strong>Monitoring only</strong>
              <small>This host reports but will never deploy anything, whatever Argus says later.</small>
            </span>
          </label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="actions">
            <button className="btn btn-primary">Make the command</button>
            <button className="btn" type="button" onClick={onClose}>Cancel</button>
          </div>
        </>
      )}
      {made && !joined && (
        <>
          <p>Run this on {made.count > 1 ? `each of the ${made.count} hosts` : "the host"} as a user who can sudo:</p>
          <pre className="snippet command">{made.command}</pre>
          <p className="muted small">
            Logged in as root on a host without sudo, as in many Alpine or Proxmox containers? Leave
            out <code>sudo</code>. Alpine needs curl first: <code>apk add curl</code>.
          </p>
          <div className="actions">
            <Copy text={made.command} />
            <button className="btn" type="button" onClick={onClose}>Done</button>
          </div>
          <p className="muted" aria-live="polite">
            {made.count > 1 && arrived.length > 0 && <>{arrived.length} of {made.count} joined: {arrived.map((h) => h.name).join(", ")}. </>}
            {left > 0
              ? <>Waiting for {made.count > 1 ? "the hosts" : "the host"}… The code works {made.count > 1 ? `for ${made.count - arrived.length} more` : "once"}, for {Math.floor(left / 60)}m {left % 60}s more.</>
              : <>The code has expired. <button className="link-button" type="button" onClick={() => setMade(null)}>Make a new one</button>.</>}
          </p>
        </>
      )}
      {joined && (
        <>
          <p className="form-ok" role="status">
            {made.count > 1 ? <>All {made.count} joined: {arrived.map((h) => h.name).join(", ")}.</>
              : <><strong>{joined.name}</strong> joined. {hostSentence(joined)}</>}
          </p>
          <div className="actions"><button className="btn" type="button" onClick={onClose}>Done</button></div>
        </>
      )}
    </form>
  );
}

function Containers({ list, host, session, reload }) {
  const [all, setAll] = useState(false);
  const [error, setError] = useState(null);
  const ignored = host.ignored ?? [];
  const sorted = sortContainers(list, ignored);
  const shown = all ? sorted : sorted.slice(0, FIRST_CONTAINERS);
  const toggle = async (name, on) => {
    try {
      await send("PUT", `/api/companions/${host.id}/ignored`, { container: name, ignored: on }, session.csrf);
      reload();
    } catch (e) {
      setError(e.message);
    }
  };
  if (!list.length) return <p className="muted small">Docker runs, with no containers.</p>;
  return (
    <>
      <ul className="host-list">
        {shown.map((c) => {
          const off = ignored.includes(c.name);
          const tone = off ? "unknown" : containerTrouble(c) ? "down" : c.health === "starting" ? "failing" : "up";
          return (
            <li key={c.name} className={off ? "muted" : undefined}>
              <span className={`dot dot-${tone}`} />
              <span className="host-item">{c.name}</span>
              <span className={`st-${tone} host-note`}>{off ? `ignored, ${c.health || c.state}` : c.health || c.state}</span>
              <span className="muted host-sub mono">{c.image}</span>
              {session?.signed_in && (off || containerTrouble(c)) && (
                <button className="btn btn-small" onClick={() => toggle(c.name, !off)}>
                  {off ? "Stop ignoring" : "Ignore"}
                </button>
              )}
            </li>
          );
        })}
      </ul>
      {sorted.length > FIRST_CONTAINERS && (
        <button className="link-button small" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${sorted.length}`}
        </button>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </>
  );
}

function Disks({ disks }) {
  return (
    <ul className="host-list">
      {disks.map((d) => {
        const share = d.total ? d.used / d.total : 0;
        return (
          <li key={d.mount} className="disk">
            <span className="host-item mono">{d.mount}</span>
            <span className={`st-${diskTone(d)} host-note`}>{Math.round(share * 100)}%</span>
            <span className="bar" aria-hidden="true">
              <span className={`fill-${diskTone(d)}`} style={{ width: `${Math.min(100, share * 100)}%` }} />
            </span>
            <span className="muted host-sub">{bytes(d.total - d.used)} free of {bytes(d.total)}</span>
          </li>
        );
      })}
    </ul>
  );
}

const STACK = {
  deployed: ["up", "deployed"], pending: ["failing", "deploying"], failed: ["down", "failed"],
  refused: ["down", "refused by policy"], rolled_back: ["down", "rolled back"],
  read_only: ["unknown", "not deployed: monitoring only"],
  link: ["accent", "waiting to be linked"],
};

// A stack's secrets: names are shown, values are write-only.
function Secrets({ stack, session, onSaved }) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState(null);
  const change = async (env) => {
    setError(null);
    try {
      await send("PUT", "/api/deploys/secrets", { path: stack.path, env }, session.csrf);
      setKey(""); setValue("");
      onSaved();
    } catch (e) {
      setError(e.message);
    }
  };
  return (
    <div className="secrets">
      {stack.secrets.length > 0 && (
        <ul className="host-list">{stack.secrets.map((k) => (
          <li key={k}><span className="dot dot-accent" /><span className="host-item mono">{k}</span>
            <button className="link-button small" onClick={() => change({ [k]: null })}>remove</button></li>
        ))}</ul>
      )}
      <form className="field-row" onSubmit={(e) => { e.preventDefault(); change({ [key]: value }); }}>
        <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="NAME" aria-label="Secret name"
               pattern="[A-Za-z_][A-Za-z0-9_]*" required spellCheck="false" />
        <input type="password" value={value} onChange={(e) => setValue(e.target.value)} placeholder="value"
               aria-label="Secret value" autoComplete="off" />
        <button className="btn">Set</button>
      </form>
      <small className="muted">Written to the stack's .env on the host (root only). Changing one redeploys the stack.</small>
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

function Stacks({ stacks, session, reload }) {
  const [open, setOpen] = useState(null);
  return (
    <ul className="host-list">
      {stacks.map((st) => {
        const [tone, word] = STACK[st.status] ?? ["unknown", st.status];
        return (
          <li key={st.path} className="stack">
            <span className={`dot dot-${tone}`} />
            <span className="host-item">{st.name}</span>
            <span className={`st-${tone} host-note`}>{word}</span>
            <span className="muted host-sub mono">{st.path}{st.at ? ` · ${agoText(Date.now() / 1000 - st.at)}` : ""}</span>
            {st.status === "link" && (
              <span className="host-wide small muted">Runs from <code>{st.held}</code>, outside Argus. Link it below
                to deploy it from git where it is.</span>
            )}
            {st.detail && st.status === "deployed" && (
              <span className="host-wide small muted">{st.detail.charAt(0).toUpperCase() + st.detail.slice(1)}.</span>
            )}
            {st.detail && st.status !== "deployed" && st.status !== "link" && <pre className="host-wide log small">{st.detail}</pre>}
            {session?.signed_in && (
              <button className="link-button small host-wide secrets-toggle" onClick={() => setOpen(open === st.path ? null : st.path)}>
                Secrets ({st.secrets.length})
              </button>
            )}
            {open === st.path && <div className="host-wide"><Secrets stack={st} session={session} onSaved={reload} /></div>}
          </li>
        );
      })}
    </ul>
  );
}

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const RUN_TONE = { ok: "up", failed: "down", running: "accent", granted: "accent", rebooting: "accent" };

function ExemptRows({ guests, session, mark }) {
  return (
    <ul className="rows">{guests.map((g) => (
      <li key={g.vmid}>
        <span className="dot dot-hollow" />
        <span className="row-main">{g.name || g.vmid} <span className="muted mono">{g.type === "lxc" ? "CT" : "VM"} {g.vmid}</span></span>
        <span className="row-note" />
        <span className="row-act">{session?.signed_in && (
          <button type="button" className="btn btn-small btn-ghost" onClick={() => mark(g, false)}
                  title="Count it again: the server waits for its backup">Count it again</button>
        )}</span>
      </li>
    ))}</ul>
  );
}

function GuestsNet({ host, days, session, reload }) {
  const [error, setError] = useState(null);
  const guests = guestsNet(host, days);
  const short = guests.filter((g) => !g.ok);
  const exempt = guests.filter((g) => g.exempt);
  const mark = async (g, on) => {
    try {
      await send("PUT", `/api/companions/${host.id}/ignored`, { container: `nobackup:${g.vmid}`, ignored: on }, session.csrf);
      reload?.();
    } catch (e) {
      setError(e.message);
    }
  };
  return (
    <div className="guests-net">
      <p className={`small ${short.length ? "st-failing" : "muted"}`}>
        {short.length === 0 ? `All ${guests.length - exempt.length} guests have a backup from the last ${days} days.`
          : `${short.length} of ${guests.length} guests have no backup from the last ${days} days, so Argus will not patch this server yet:`}
      </p>
      {short.length === 0 && exempt.length > 0 && (
        <details className="quiet-details">
          <summary className="small muted">{exempt.length} left out of backups on purpose: {exempt.map((g) => g.name || g.vmid).join(", ")}</summary>
          <ExemptRows guests={exempt} session={session} mark={mark} />
        </details>
      )}
      {short.length > 0 && (
        <ul className="rows">{[...short, ...exempt.filter((g) => g.ok)].map((g) => (
          <li key={g.vmid}>
            <span className={`dot dot-${g.ok ? "hollow" : "failing"}`} />
            <span className="row-main">{g.name || g.vmid} <span className="muted mono">{g.type === "lxc" ? "CT" : "VM"} {g.vmid}</span>
              <span className="row-sub muted">{g.exempt ? "needs no backup" : g.age === null ? "no backup this server can see" : `backed up ${agoText(g.age)}`}</span></span>
            <span className="row-note" />
            <span className="row-act">{session?.signed_in && (
              <button type="button" className="btn btn-small btn-ghost" onClick={() => mark(g, !g.exempt)}
                      title="Left out of backups on purpose: it does not hold the server back">
                {g.exempt ? "Count it again" : "It needs no backup"}</button>
            )}</span>
          </li>
        ))}</ul>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

function PatchForm({ host, session, refs, backups, onSaved, onCancel, reload }) {
  const suggested = host.patching.suggested_backup;
  const server = !!host.report?.proxmox;
  const [p, setP] = useState(() => {
    const plan = host.patching.plan;
    // A guest whose Proxmox backup Argus can see starts with it chosen; a Proxmox
    // server's safety net is always its guests' backups.
    if (server) return { ...plan, backup: "pve:guests" };
    return !plan.backup && !plan.no_backup && suggested ? { ...plan, backup: suggested } : plan;
  });
  const [error, setError] = useState(null);
  const set = (k, v) => setP({ ...p, [k]: v });
  const save = async (e) => {
    e.preventDefault();
    try {
      // The choice shown is the choice saved: "Argus, every week" means on.
      const outside = net === "outside" ? (p.outside_backup ?? "").trim() : "";
      if (net === "outside" && !outside) throw new Error("say where it is backed up");
      await send("PUT", `/api/patching/${host.name}`, { ...p, day: Number(p.day), enabled: !p.elsewhere,
        outside_backup: outside, no_backup: net === "none" }, session.csrf);
      onSaved();
    } catch (err) {
      setError(err.message);
    }
  };
  const who = p.elsewhere ? "elsewhere" : "argus";
  // No backup of this host anywhere Argus looks: say where one comes from.
  const seen = backups.find((b) => b.ref === suggested);
  const noneSeen = !server && !p.backup && !(seen && seen.at);
  const pick = (w) => setP({ ...p, elsewhere: w === "elsewhere", enabled: w === "argus" });
  // The safety net: a backup Argus sees, one it cannot (the owner's word), or none needed.
  const [net, setNet] = useState(() => (host.patching.plan.outside_backup ? "outside"
    : host.patching.plan.no_backup ? "none" : "backup"));
  const pickNet = (n) => {
    setNet(n);
    setP({ ...p, no_backup: n === "none", outside_backup: n === "outside" ? p.outside_backup : "",
           backup: n === "backup" ? p.backup : "" });
  };
  const backupSelect = (
    <select value={p.backup} onChange={(e) => set("backup", e.target.value)} disabled={p.no_backup} aria-label="Its backup">
      <option value="">{p.no_backup ? "None needed" : "Choose its backup…"}</option>
      {backups.length > 0 && (
        <optgroup label="Proxmox backups">
          {backups.map((b) => <option key={b.ref} value={b.ref}>{backupLabel(b)}{b.ref === suggested ? " (this host)" : ""}</option>)}
        </optgroup>
      )}
      {refs.length > 0 && (
        <optgroup label="Jobs and timers that report to Argus">
          {refs.map((r) => <option key={r} value={r}>{r.replace(":", ": ")}</option>)}
        </optgroup>
      )}
    </select>
  );
  const days = (
    <span className="pf-inline">no older than
      <input type="number" min="1" max="366" value={p.backup_days} aria-label="Days"
             onChange={(e) => set("backup_days", Number(e.target.value))} required disabled={p.no_backup} /> days</span>
  );
  return (
    <form className="patch-form pf" onSubmit={save}>
      <div className="pf-group">
        <h4 className="pf-title">Who patches it</h4>
        <div className="pf-choice" role="radiogroup">
          <label className="pf-option"><input type="radio" checked={who === "argus"} onChange={() => pick("argus")} />
            <span><strong>Argus, every week</strong><small>In a window you choose, one host at a time.</small></span></label>
          <label className="pf-option"><input type="radio" checked={who === "elsewhere"} onChange={() => pick("elsewhere")} />
            <span><strong>Something else</strong><small>Ansible, unattended-upgrades, your hypervisor. Argus stays out of its way.</small></span></label>
        </div>
      </div>

      {who === "elsewhere" && (
        <div className="pf-group">
          <h4 className="pf-title">The job that patches it</h4>
          <div className="pf-row">
            <select value={p.elsewhere_job} onChange={(e) => set("elsewhere_job", e.target.value)} aria-label="Job">
              <option value="">None reports to Argus</option>
              {refs.map((r) => <option key={r} value={r}>{r.replace(":", ": ")}</option>)}
            </select>
          </div>
          <p className="pf-note">Optional. Named, the host page shows how its last run went.</p>
        </div>
      )}

      {who === "argus" && <>
        <div className="pf-group">
          <h4 className="pf-title">When</h4>
          <div className="pf-row">
            <select value={p.day} onChange={(e) => set("day", Number(e.target.value))} aria-label="Day">
              {DAYS.map((d, i) => <option key={d} value={i}>{d}s</option>)}
            </select>
            <span className="pf-inline">at</span>
            <input type="time" value={p.time} onChange={(e) => set("time", e.target.value)} required aria-label="Time" />
            <span className="pf-note">on the host's own clock</span>
          </div>
        </div>

        <div className="pf-group">
          <h4 className="pf-title">What</h4>
          <div className="pf-row">
            <select value={p.level} onChange={(e) => set("level", e.target.value)} aria-label="Updates">
              <option value="full">All updates</option>
              <option value="security">Security updates only</option>
            </select>
            <label className="pf-check"><input type="checkbox" checked={p.reboot} onChange={(e) => set("reboot", e.target.checked)} />
              Reboot when an update needs it</label>
          </div>
        </div>

        <div className="pf-group">
          <h4 className="pf-title">Safety net</h4>
          {server ? (
            <>
              <div className="pf-row"><span>Every guest on it backed up,</span>{days}</div>
              <GuestsNet host={host} days={p.backup_days} session={session} reload={reload} />
            </>
          ) : (
            <>
              <div className="pf-net" role="radiogroup" aria-label="Safety net">
                <label className="pf-check"><input type="radio" checked={net === "backup"} onChange={() => pickNet("backup")} />
                  A backup Argus can see</label>
                {net === "backup" && <div className="pf-row pf-sub">{backupSelect}{days}</div>}
                <label className="pf-check"><input type="radio" checked={net === "outside"} onChange={() => pickNet("outside")} />
                  Backed up outside Argus</label>
                {net === "outside" && (
                  <div className="pf-sub">
                    <input className="pf-text" value={p.outside_backup ?? ""} required maxLength={200}
                           placeholder="Sunday snapshot on another Proxmox, to the NAS"
                           onChange={(e) => set("outside_backup", e.target.value)} aria-label="Where it is backed up" />
                    <p className="pf-note">Argus cannot check it, so it takes your word and shows it as such.</p>
                  </div>
                )}
                <label className="pf-check"><input type="radio" checked={net === "none"} onChange={() => pickNet("none")} />
                  It needs no backup: I could rebuild it</label>
              </div>
              {net === "backup" && noneSeen && (
                <p className="pf-hint">
                  Argus sees no backup of {host.name}. {seen ? "Add it to a backup job in Proxmox (Datacenter, Backup); it is chosen here after the next run." : "Pick a job or timer that reports when its backup finishes, or choose Backed up outside Argus."}
                </p>
              )}
              {net === "backup" && (
                <details className="about">
                  <summary>Restore test (optional){p.restore ? `: ${p.restore.replace(":", ": ")}` : ""}</summary>
                  <div className="pf-row">
                    <select value={p.restore} onChange={(e) => set("restore", e.target.value)} aria-label="Restore test">
                      <option value="">None</option>
                      {refs.map((r) => <option key={r} value={r}>{r.replace(":", ": ")}</option>)}
                    </select>
                  </div>
                  <p className="pf-note">A job that proves the backup restores. Argus nudges you when none ran in 35 days.</p>
                </details>
              )}
            </>
          )}
          <details className="about"><summary>How Argus decides a host is safe to patch</summary><p className="muted small">
            Argus patches a host only when it can be brought back: its backup is more recent than the
            days you set, or it has a ZFS root Argus snapshots first, or you said it needs no backup.
            A Proxmox guest's own vzdump backup counts by itself; otherwise pick a job or timer that
            reports to Argus when your backup finishes.
          </p></details>
        </div>
      </>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="actions">
        <button className="btn btn-primary">Save</button>
        <button className="btn" type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function Patching({ host, session, refs, backups, reload }) {
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState(null);
  const pt = host.patching;
  if (!pt) return null;
  const last = pt.runs[0];
  const now = async () => {
    try {
      await send("POST", `/api/patching/${host.name}/now`, {}, session.csrf);
      reload();
    } catch (e) {
      setError(e.message);
    }
  };
  return (
    <section className="patching">
      {pt.plan.elsewhere ? (
        <p className="small">
          Patched by something else{pt.elsewhere ? <>: {pt.elsewhere.title}{pt.elsewhere.schedule && `, ${pt.elsewhere.schedule}`}.{" "}
            <span className={`st-${{ ok: "up", failed: "down", late: "failing" }[pt.elsewhere.status] ?? "unknown"}`}>
              {pt.elsewhere.at ? `Last run ${pt.elsewhere.status === "ok" ? "ok" : pt.elsewhere.status} ${agoText(Date.now() / 1000 - pt.elsewhere.at)}` : "No run reported yet"}
            </span>{pt.elsewhere.summary && <span className="muted">: {pt.elsewhere.summary}</span>}</> : "."}
        </p>
      ) : (
        <p className="small">
          {pt.plan.enabled
            ? <>{pt.allowed[0].toUpperCase() + pt.allowed.slice(1)}, {DAYS[pt.plan.day]}s at {pt.plan.time} (host time).</>
            : "Not patched by Argus."}{" "}
          {pt.safety.ok || pt.plan.backup ? <span className="muted">Safety net: {pt.safety.net}.</span> : (
            <span className="muted">Safety net: none.{" "}
              {session?.signed_in && !host.read_only
                ? <><button className="link-button" onClick={() => setEditing(true)}>Link a backup</button> to patch this host.</>
                : "Link a backup to patch this host."}</span>
          )}
          {pt.plan.restore && !pt.safety.tested && <span className="muted"> No restore test in the last 35 days.</span>}
          {pt.plan.enabled && !pt.plan.restore && <span className="muted"> Never restore-tested: worth a try.</span>}
        </p>
      )}
      {host.report?.proxmox && !editing && <GuestsNet host={host} days={pt.plan.backup_days || 8} session={session} reload={reload} />}
      {pt.skipped && <p className="small st-failing">Not patched: {pt.skipped}.</p>}
      {last && (
        <p className="small">
          <span className={`st-${RUN_TONE[last.result] ?? "unknown"}`}>
            {last.result === "granted" ? "starting" : last.result}
          </span>{" "}
          {agoText(Date.now() / 1000 - last.requested)}{last.summary && `: ${last.summary}`}
        </p>
      )}
      {last?.log && last.result === "failed" && <pre className="log small">{last.log.slice(-1500)}</pre>}
      {pt.requested && <p className="small muted">Patching on its next report, within a minute.</p>}
      {session?.signed_in && !host.read_only && !editing && (
        <div className="actions">
          <button className={`btn btn-small${pt.plan.enabled || pt.plan.elsewhere ? "" : " btn-primary"}`} onClick={() => setEditing(true)}>
            {pt.plan.enabled || pt.plan.elsewhere ? "Change how it is patched" : "Choose a window"}</button>
          {!pt.plan.elsewhere && (
            <button className="btn btn-small" onClick={now} disabled={pt.requested || last?.result === "running"}>Patch now</button>
          )}
        </div>
      )}
      {editing && <PatchForm host={host} session={session} refs={refs} backups={backups} reload={reload}
                             onSaved={() => { setEditing(false); reload(); }} onCancel={() => setEditing(false)} />}
      {error && (
        <p className="form-error" role="alert">{error}{" "}
          {!editing && !pt.safety.ok && <button className="link-button" onClick={() => { setError(null); setEditing(true); }}>Link a backup</button>}</p>
      )}
    </section>
  );
}

// A section of a host's page: a title, an optional "About" that explains it, and its content.
function Section({ title, about, aside, children }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="hsec">
      <header className="hsec-head">
        <h3 className="eyebrow">{title}</h3>
        {about && (
          <button className="about-toggle" aria-expanded={open} onClick={() => setOpen(!open)} title="About this">
            {open ? "Hide" : "About"}
          </button>
        )}
        {aside && <span className="hsec-aside">{aside}</span>}
      </header>
      {open && <p className="about-text muted small">{about}</p>}
      {children}
    </section>
  );
}

const INSTALL_NOTE = { waiting: "adding Argus…", running: "adding Argus…", ok: "Argus added", failed: "could not add Argus" };

// Add Argus to guests through their server; shared by the list and the server's Guests tab.
function useGuestInstall(server, session, reload) {
  const [error, setError] = useState(null);
  const installs = server.guest_installs ?? {};
  const canAdd = (g, has) => session?.signed_in && !server.read_only && g.status === "running" && !has
    && !["waiting", "running", "ok"].includes(installs[g.vmid]?.state);
  const add = async (list) => {
    setError(null);
    for (const g of list) {
      try {
        await send("POST", `/api/companions/${server.id}/guests/${g.vmid}/install`,
          { url: window.location.origin }, session.csrf);
      } catch (e) {
        setError(`${g.name || g.vmid}: ${e.message}`);
      }
    }
    reload();
  };
  const skipped = new Set((server.ignored ?? []).filter((x) => x.startsWith("guest:")).map((x) => Number(x.slice(6))));
  const skip = async (g, on) => {
    setError(null);
    try {
      await send("PUT", `/api/companions/${server.id}/ignored`, { container: `guest:${g.vmid}`, ignored: on }, session.csrf);
    } catch (e) {
      setError(`${g.name || g.vmid}: ${e.message}`);
    }
    reload();
  };
  return { installs, canAdd: (g, has) => canAdd(g, has) && !skipped.has(g.vmid), add, error, skipped, skip };
}

function backupText(g, now = Date.now() / 1000) {
  return g.backup ? `backed up ${agoText(now - g.backup.at)}` : "no backup";
}

function backupTone(g, now = Date.now() / 1000) {
  return !g.backup ? "failing" : now - g.backup.at < 8 * 86400 ? "up" : "failing";
}

// A guest's missing or old backup: nothing once the owner said it needs none,
// a warning when it holds back a patch plan that is on, a quiet note otherwise.
function backupTag(host, guest) {
  if (!guest || backupTone(guest) === "up" || host.patching?.plan?.no_backup) return [];
  return [[host.patching?.plan?.enabled ? "failing" : "unknown", backupText(guest)]];
}

function Tags({ items, empty = "all good" }) {
  // Fine is the normal state: say it quietly, so the rows that need a look stand out.
  if (!items.length) return <span className="tag-none">{empty}</span>;
  return <span className="tags">{items.map(([tone, text]) => <span key={text} className={`tag tag-${tone}`}>{text}</span>)}</span>;
}

const open = (name, tab) => { window.location.hash = `/hosts?host=${encodeURIComponent(name)}${tab ? `&tab=${tab}` : ""}`; };

function HostRow({ host, guest, child, toggle }) {
  const [tone, word] = STATUS[host.status];
  const [ptone, ptext] = patchLabel(host);
  return (
    <div className={`fleet-row${child ? " fleet-child" : ""}`}>
      <span className="fleet-name">
        {toggle ? (
          <button className="fold" aria-expanded={toggle.open} onClick={toggle.flip}
                  title={toggle.open ? "Hide its guests" : "Show its guests"}>{toggle.open ? "▾" : "▸"}</button>
        ) : !child && <span className="fold-space" />}
        <span className={`dot dot-${tone}`} title={word} />
        <a className="fleet-title" href={`#/hosts?host=${encodeURIComponent(host.name)}`}>
          <strong>{host.name}</strong><span className="muted small">{hostSub(host, guest)}</span></a>
      </span>
      <span className="fleet-tags">
        <Tags items={[...attention(host), ...backupTag(host, guest)]} /></span>
      <span className="fleet-patch small">
        <span className={`st-${ptone}`}>{ptext}</span>
        {host.patching?.plan?.outside_backup ? <span className="muted">backed up outside Argus</span>
          : host.patching?.plan?.no_backup ? <span className="muted">no backup needed</span>
          : guest?.backup && backupTone(guest) === "up" && <span className="muted">backup {agoText(Date.now() / 1000 - guest.backup.at)}</span>}
      </span>
      <span className="fleet-seen small muted">{host.last_seen ? agoText(host.age) : "waiting"}</span>
      <a className="fleet-go" href={`#/hosts?host=${encodeURIComponent(host.name)}`} aria-label={`Open ${host.name}`}>›</a>
    </div>
  );
}

// `command` in a help step becomes its own copyable line.
function withCode(text) {
  return text.split(/`([^`]+)`/).map((part, i) => (i % 2
    ? <span key={i} className="help-cmd"><code>{part}</code><Copy text={part} /></span> : part));
}

// Why adding Argus to a guest failed, what to do next, and the raw output.
function InstallHelp({ guest, install }) {
  const help = installHelp(install.output, guest);
  return (
    <div className="install-help">
      <strong>{help.title}</strong>
      {help.lead && <p className="install-lead muted">{help.lead}</p>}
      <ol>{help.steps.map((s) => <li key={s}>{withCode(s)}</li>)}</ol>
      {help.manual && (
        <p><a className="btn btn-small" href={`#/hosts?add=${encodeURIComponent(guestHostName(guest.name || `guest-${guest.vmid}`))}`}>
          Add it by hand instead</a> <span className="muted small">Run Add a host's command in the guest; it joins under this name.</span></p>
      )}
      {install.output && <details><summary className="small muted">What it printed</summary><pre className="log">{install.output}</pre></details>}
    </div>
  );
}

function GuestRow({ guest, install, canAdd, onAdd, skipped, onSkip }) {
  const fine = backupTone(guest) === "up";
  const [why, setWhy] = useState(false);
  const failed = install?.state === "failed" && !skipped;
  return (
    <>
    <div className="fleet-row fleet-child fleet-bare">
      <span className="fleet-name">
        <span className="dot dot-hollow" title="no Argus" />
        <span className="fleet-title"><span>{guest.name || "(no name)"}</span>
          <span className="muted small">{guest.type === "lxc" ? "CT" : "VM"} {guest.vmid} · {guest.status}</span></span>
      </span>
      <span className="fleet-tags">
        {skipped ? <span className="muted small">skipped</span> : <>
          {install && <span className={`tag tag-${install.state === "failed" ? "down" : "unknown"}`}>{INSTALL_NOTE[install.state]}</span>}
          {fine ? <span className="muted small">backup {agoText(Date.now() / 1000 - guest.backup.at)}</span>
            : <span className="tag tag-failing">{backupText(guest)}</span>}
        </>}
      </span>
      <span className="fleet-act">
        {failed && <button className="btn btn-small" aria-expanded={why} onClick={() => setWhy(!why)}>{why ? "Hide" : "Why?"}</button>}
        {canAdd && !skipped && <button className="btn btn-small" onClick={() => onAdd([guest])}>{install?.state === "failed" ? "Try again" : "Add Argus"}</button>}
        {onSkip && <button className="btn btn-small btn-ghost" onClick={() => onSkip(guest, !skipped)}
                           title={skipped ? "Count it again" : "It cannot or should not run Argus (an appliance, a switched-off guest)"}>
          {skipped ? "Unskip" : "Skip"}</button>}
      </span>
    </div>
    {failed && why && <div className="fleet-child fleet-help"><InstallHelp guest={guest} install={install} /></div>}
    </>
  );
}

function remembered(key, initial) {
  try { const v = localStorage.getItem(key); return v === null ? initial : v === "1"; } catch { return initial; }
}

function remember(key, value) {
  try { localStorage.setItem(key, value ? "1" : "0"); } catch { /* private window: forget it */ }
}

// A Proxmox server and its guests: those with Argus as hosts under it, the rest
// folded into one line until asked for.
function ServerGroup({ row, session, reload }) {
  const { host, guests } = row;
  const gi = useGuestInstall(host, session, reload);
  const [open, setOpen] = useState(() => remembered(`argus.fold.${host.name}`, true));
  const [bareOpen, setBareOpen] = useState(false);
  const flip = () => { remember(`argus.fold.${host.name}`, !open); setOpen(!open); };
  const withArgus = guests.filter((g) => g.host);
  const bare = guests.filter((g) => !g.host && !gi.skipped.has(g.guest.vmid));
  const skippedRows = guests.filter((g) => !g.host && gi.skipped.has(g.guest.vmid));
  const addable = bare.filter((g) => gi.canAdd(g.guest, false)).map((g) => g.guest);
  const busy = bare.filter((g) => ["waiting", "running", "failed"].includes(gi.installs[g.guest.vmid]?.state));
  // Guests being added, or that failed, stay in sight.
  const shown = bareOpen ? [...bare, ...skippedRows] : busy;
  return (
    <div className="fleet-group">
      <HostRow host={host} toggle={{ open, flip }} />
      {open && <>
        {withArgus.map((g) => <HostRow key={g.host.id} host={g.host} guest={g.guest} child />)}
        {bare.length + skippedRows.length > 0 && (
          <div className="fleet-row fleet-child fleet-fold">
            <span className="fleet-name">
              <span className="dot dot-hollow" />
              <span className="muted small">
                {bare.length ? `${bare.length} guest${bare.length === 1 ? "" : "s"} without Argus` : "Every guest has Argus"}
                {skippedRows.length > 0 && `, ${skippedRows.length} skipped`}</span>
            </span>
            <span className="fleet-act">
              {addable.length > 0 && session?.signed_in && (
                <button className="btn btn-small" onClick={() => gi.add(addable)}>
                  {addable.length === 1 ? `Add Argus to ${addable[0].name || addable[0].vmid}` : `Add Argus to all ${addable.length}`}</button>
              )}
              <button className="btn btn-small" onClick={() => setBareOpen(!bareOpen)}>{bareOpen ? "Hide" : "Show"}</button>
            </span>
          </div>
        )}
        {shown.map((g) => (
          <GuestRow key={g.guest.vmid} guest={g.guest} install={gi.installs[g.guest.vmid]}
                    canAdd={gi.canAdd(g.guest, false)} onAdd={gi.add} skipped={gi.skipped.has(g.guest.vmid)}
                    onSkip={session?.signed_in && !host.read_only ? gi.skip : null} />
        ))}
      </>}
      {gi.error && <p className="form-error fleet-error" role="alert">{gi.error}</p>}
    </div>
  );
}

function HostList({ hosts, session, reload }) {
  const rows = hostTree(hosts);
  return (
    <div className="fleet" role="list">
      <div className="fleet-row fleet-headrow" aria-hidden="true">
        <span className="eyebrow">host</span><span className="eyebrow">needs a look</span>
        <span className="eyebrow">patching</span><span className="eyebrow">reported</span><span />
      </div>
      {rows.map((row) => row.host.report?.proxmox
        ? <ServerGroup key={row.host.id} row={row} session={session} reload={reload} />
        : <div className="fleet-group" key={row.host.id}><HostRow host={row.host} /></div>)}
    </div>
  );
}

// The Guests tab of a Proxmox server.
function Guests({ host, hostNames, session, reload }) {
  const px = host.report.proxmox;
  const gi = useGuestInstall(host, session, reload);
  const has = (g) => hostNames.has(guestHostName(g.name || ""));
  const addable = px.guests.filter((g) => gi.canAdd(g, has(g)));
  return (
    <Section title={`guests (${px.guests.length})`}
      about={<>Your apps run in these VMs and containers. Add Argus to each to see its containers, watch it and
        patch it; the backup shown is its safety net, linked for you. <strong>Add Argus</strong> has this server
        install it in the guest: containers always work, VMs need the QEMU guest agent.</>}
      aside={addable.length > 1 && <button className="btn btn-small" onClick={() => gi.add(addable)}>Add Argus to all {addable.length}</button>}>
      <ul className="rows">{px.guests.map((g) => {
        const inst = gi.installs[g.vmid];
        const argus = has(g);
        return (
          <li key={`${g.type}${g.vmid}`}>
            <span className={`dot dot-${argus ? "up" : "hollow"}`} />
            <span className="row-main">
              {argus ? <a href={`#/hosts?host=${encodeURIComponent(guestHostName(g.name))}`}>{g.name}</a> : g.name || "(no name)"}
              <span className="muted mono"> {g.type === "lxc" ? "CT" : "VM"} {g.vmid}</span>
              <span className="row-sub muted">{g.status} · {g.backup ? `backed up ${agoText(Date.now() / 1000 - g.backup.at)} to ${g.backup.storage} (${bytes(g.backup.size)})` : "no backup this server can see"}</span>
            </span>
            <span className={`row-note ${argus ? "st-up" : inst?.state === "failed" ? "st-failing" : "muted"}`}>
              {argus ? "has Argus" : gi.skipped.has(g.vmid) ? "skipped" : inst ? INSTALL_NOTE[inst.state] : ""}</span>
            <span className="row-act">
              {gi.canAdd(g, argus) && <button className="btn btn-small" onClick={() => gi.add([g])}>{inst?.state === "failed" ? "Try again" : "Add Argus"}</button>}
              {!argus && session?.signed_in && !host.read_only && (
                <button className="btn btn-small btn-ghost" onClick={() => gi.skip(g, !gi.skipped.has(g.vmid))}>
                  {gi.skipped.has(g.vmid) ? "Unskip" : "Skip"}</button>
              )}
            </span>
            {inst?.state === "failed" && !argus && !gi.skipped.has(g.vmid) && (
              <div className="row-wide"><InstallHelp guest={g} install={inst} /></div>
            )}
          </li>
        );
      })}</ul>
      {px.error && <p className="st-failing small">{px.error}</p>}
      {gi.error && <p className="form-error" role="alert">{gi.error}</p>}
    </Section>
  );
}

function NotFromGit({ host, session, reload }) {
  const [openP, setOpenP] = useState(null);
  if (!host.adoptable?.length) return null;
  return (
    <Section title="running here, not deployed by Argus"
      about={<>Compose projects started by hand or by another tool. <strong>Deploy from git</strong> puts each
        one's compose file in your deploy repo; its secrets stay in Argus. From then on, a merge redeploys it in
        the same folder. If nothing changed, nothing restarts.</>}>
      <ul className="rows">{host.adoptable.map((p) => (
        <li key={p.project}>
          <span className="dot dot-unknown" />
          <span className="row-main">{p.project}<span className="row-sub muted mono">{p.dir}</span></span>
          <span className="row-note" />
          <span className="row-act">
            {session?.signed_in && openP !== p.project && (
              <button className="btn btn-small" onClick={() => setOpenP(p.project)}>{p.link ? "Link" : "Deploy from git"}</button>
            )}
          </span>
          {openP === p.project && (
            <div className="row-wide">
              <Adopt host={host.name} project={p} session={session}
                     onDone={() => { setOpenP(null); reload(); }} onCancel={() => setOpenP(null)} />
            </div>
          )}
        </li>
      ))}</ul>
    </Section>
  );
}

// Reinstall a host's companion: a Proxmox guest's server does it for you; any
// other host gets the command, already trimmed (no code: the host stays as it is).
function guestOf(host, hosts) {
  for (const s of hosts) {
    if (s.id === host.id || s.read_only || s.status !== "ok") continue;
    const g = (s.report?.proxmox?.guests ?? []).find((x) => x.status === "running" && x.name && guestHostName(x.name) === host.name);
    if (g) return { server: s, guest: g };
  }
  return null;
}

function Reinstall({ host, hosts, session, updateCommand, onClose }) {
  const [asked, setAsked] = useState(null);
  const [error, setError] = useState(null);
  const via = guestOf(host, hosts);
  const live = via && (hosts.find((h) => h.id === via.server.id)?.guest_installs ?? {})[via.guest.vmid];
  const ask = async () => {
    setError(null);
    try { setAsked(await send("POST", `/api/companions/${host.id}/reinstall`, { url: window.location.origin }, session.csrf)); }
    catch (e) { setError(e.message); }
  };
  const state = asked && live?.reinstall ? live.state : asked ? "waiting" : null;
  return (
    <div className="reinstall">
      {via ? (
        <>
          <p className="small">{via.server.name} can reinstall it inside {via.guest.type === "lxc" ? "CT" : "VM"} {via.guest.vmid} for you. The host
            stays as it is in Argus; only the companion is replaced, with Argus's current build.</p>
          <div className="actions">
            {!state && <button className="btn btn-small btn-primary" onClick={ask}>Have {via.server.name} reinstall it</button>}
            {state && <span className={`small ${state === "ok" ? "st-up" : state === "failed" ? "st-failing" : "muted"}`}>
              {state === "ok" ? "Reinstalled. It reports the new build within a minute."
                : state === "failed" ? "That did not work; run the command below on the host instead."
                : `${via.server.name} is reinstalling it…`}</span>}
            <button className="btn btn-small" onClick={onClose}>Close</button>
          </div>
          {state === "failed" && live?.output && <pre className="log">{live.output}</pre>}
          <details className="about"><summary>Or run it on the host yourself</summary>
            <pre className="snippet command">{updateCommand}</pre><Copy text={updateCommand} /></details>
        </>
      ) : (
        <>
          <p className="small">Open a shell on {host.name} (SSH, or its console) and paste this, as root or a user who can sudo.
            The host stays as it is in Argus; only the companion is replaced.</p>
          <pre className="snippet command">{updateCommand}</pre>
          <div className="actions"><Copy text={updateCommand} /><button className="btn btn-small" onClick={onClose}>Close</button></div>
        </>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

// Uninstall (design decision 18): what happens, then the admin password twice.
function Uninstall({ host, session, reload, onClose }) {
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const go = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await send("POST", `/api/companions/${host.id}/uninstall`, { password, again }, session.csrf);
      onClose();
      reload();
    } catch (err) {
      setError(err.message);
    }
    setBusy(false);
  };
  const stacks = host.stacks ?? [];
  return (
    <form className="panel uninstall" onSubmit={go}>
      <h3 className="eyebrow">Uninstall from {host.name}</h3>
      <ol className="small">
        <li>Its companion moves the stacks Argus deployed here to <code>/opt/stacks/&lt;name&gt;</code>, compose
          file and <code>.env</code> together, so you can run them yourself. Running containers keep running.
          {stacks.length > 0 && <> Stacks here: {stacks.map((s) => s.name).join(", ")}.</>}</li>
        <li>It removes itself: the service, the program and its settings.</li>
        <li>Argus forgets {host.name}: its checks run from it, its patch plan and its deploys stop.</li>
      </ol>
      <p className="small muted">Uninstalling software on another machine is the strongest thing Argus does, so it asks for your password twice.</p>
      <div className="form-grid">
        <label className="field"><span>Admin password</span>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required autoFocus /></label>
        <label className="field"><span>The password again</span>
          <input type="password" value={again} onChange={(e) => setAgain(e.target.value)} autoComplete="current-password" required /></label>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="actions">
        <button className="btn btn-danger" disabled={busy || !password || password !== again}>
          {busy ? "Asking…" : `Uninstall from ${host.name}`}</button>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        {password && again && password !== again && <span className="small st-failing">The two passwords are not the same.</span>}
      </div>
    </form>
  );
}

// An uninstall on its way: waiting for the host, no answer, or a failed handback.
function UninstallState({ host, session, reload, onRetry }) {
  const u = host.uninstall;
  const [error, setError] = useState(null);
  if (!u) return null;
  const cancel = async () => {
    try { await send("DELETE", `/api/companions/${host.id}/uninstall`, undefined, session.csrf); reload(); }
    catch (e) { setError(e.message); }
  };
  return (
    <div className={`banner ${u.state === "failed" ? "banner-warn" : ""} uninstall-state`} role="status">
      {u.state === "failed" ? (
        <p><strong>The handback failed, so the companion stays.</strong> {withCode(u.log || "No details.")}</p>
      ) : u.stale ? (
        <p><strong>No answer from {host.name} for 15 minutes.</strong> If it is offline, run <code>{u.command}</code> on
          it, then press Remove.</p>
      ) : (
        <p><strong>Uninstalling {host.name}.</strong> Waiting for its next report: it hands its stacks back, then removes itself.</p>
      )}
      <div className="actions">
        {u.state === "failed" && <button className="btn btn-small" onClick={onRetry}>Try again</button>}
        <button className="btn btn-small" onClick={cancel}>{u.state === "failed" ? "Keep it" : "Cancel"}</button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

function HostActions({ host, session, reload, hosts, updateCommand }) {
  const [mode, setMode] = useState(null);
  const [name, setName] = useState(host.name);
  const [error, setError] = useState(null);
  if (!session?.signed_in) return null;
  const rename = async (e) => {
    e.preventDefault();
    try {
      const out = await send("PUT", `/api/companions/${host.id}/name`, { name: name.trim() }, session.csrf);
      setMode(null);
      open(out.name);
      reload();
    } catch (err) {
      setError(err.message);
    }
  };
  const remove = async () => {
    try {
      await send("DELETE", `/api/companions/${host.id}`, undefined, session.csrf);
      window.location.hash = "/hosts";
      reload();
    } catch (e) {
      setError(e.message);
    }
  };
  return (
    <div className="host-actions">
      <UninstallState host={host} session={session} reload={reload} onRetry={() => setMode("uninstall")} />
      {!mode && !host.uninstall && <>
        <button className="btn btn-small" onClick={() => { setName(host.name); setMode("rename"); }}>Rename</button>
        {!host.read_only && <button className="btn btn-small" onClick={() => setMode("reinstall")}>Reinstall companion</button>}
        {!host.read_only && <button className="btn btn-small btn-quiet-danger" onClick={() => setMode("uninstall")}>Uninstall</button>}
        <button className="btn btn-small btn-quiet-danger" onClick={() => setMode("remove")}>Remove</button>
      </>}
      {mode === "uninstall" && <Uninstall host={host} session={session} reload={reload} onClose={() => setMode(null)} />}
      {mode === "reinstall" && <Reinstall host={host} hosts={hosts} session={session} updateCommand={updateCommand} onClose={() => setMode(null)} />}
      {mode === "rename" && (
        <form className="inline-form" onSubmit={rename}>
          <input value={name} onChange={(e) => setName(e.target.value.toLowerCase())} aria-label="New name"
                 pattern="[a-z0-9][a-z0-9\-]{0,62}" required autoFocus spellCheck="false" />
          <button className="btn btn-small btn-primary">Save</button>
          <button type="button" className="btn btn-small" onClick={() => setMode(null)}>Cancel</button>
          <small className="muted">Its patch schedule and history come along. Deploys follow the new name (hosts/&lt;name&gt; in your deploy repo).</small>
        </form>
      )}
      {mode === "remove" && (
        <span className="inline-form small">
          Forget {host.name} without uninstalling, for a host that is gone? Its token stops working at once. If the
          program is still on it, run <code>sudo /usr/local/bin/argus-companion uninstall</code> there{host.read_only ? "" : ", or use Uninstall instead"}.
          <button className="btn btn-small" onClick={remove}>Remove</button>
          <button className="btn btn-small" onClick={() => setMode(null)}>Keep</button>
        </span>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

// Ports no check covers, each a check one press away, added here without leaving the host.
function NotWatched({ suggestions, session, reload }) {
  const [busy, setBusy] = useState(null);
  const [done, setDone] = useState(null);
  const [error, setError] = useState(null);
  const watch = async (g, page) => {
    setBusy(g.port);
    setError(null);
    try {
      const config = await send("GET", "/api/config");
      const entry = { ...g.check, severity: page ? "critical" : "warning", interval: 60 };
      await send("POST", "/api/config/entries", { kind: "checks", entry, replace: null, base: config.base }, session.csrf);
      setDone(`${entry.name} is watched${page ? "; Argus alerts you when it goes down" : ""}.`);
      reload();
    } catch (e) {
      setError(`${g.container}: ${e.message}`);
    }
    setBusy(null);
  };
  const apps = suggestions.filter((g) => !g.internal);
  const plumbing = suggestions.filter((g) => g.internal);
  const row = (g) => (
    <li key={g.port}><span className="dot dot-unknown" />
      <span className="row-main">{g.container} <span className="muted mono">:{g.port}</span>
        <span className="row-sub muted"><span className="kind">{g.check.kind.toUpperCase()}</span>{" "}
          <span className="mono">{g.check.target}</span></span></span>
      <span className="row-note" />
      <span className="row-act">{session?.signed_in && <>
        <button className="btn btn-small" disabled={busy !== null} onClick={() => watch(g, false)}>Watch</button>
        <button className="btn btn-small" disabled={busy !== null} onClick={() => watch(g, true)}>Watch and alert me</button>
      </>}</span></li>
  );
  return (
    <Section title="not watched yet"
      about={<>Ports these containers publish that no check covers. Argus tries each one once to pick the check.
        A port that answers like a web app gets an HTTP check, which proves the app answers. Any other port gets a TCP check, which proves it is open.
        <strong> Watch</strong> shows a problem in Argus; <strong>Watch and alert me</strong> also sends an alert.
        A good rule: alert for what you or others use, watch the rest. Change either later on the Checks page.</>}>
      {done && <p className="form-ok small" role="status">{done} <a href="#/">See it on Checks</a></p>}
      <ul className="rows">{apps.map(row)}</ul>
      {plumbing.length > 0 && (
        <details className="plumbing">
          <summary className="small muted">Usually not worth watching ({plumbing.length}): exporters, brokers and other plumbing</summary>
          <ul className="rows">{plumbing.map(row)}</ul>
        </details>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </Section>
  );
}

// Services whose last run failed. Ignore sets one aside (a login message that
// cannot reach the internet, a helper for a service that is off on purpose).
function FailedUnits({ host, session, reload }) {
  const [error, setError] = useState(null);
  const ignored = host.ignored ?? [];
  const toggle = async (u, on) => {
    try {
      await send("PUT", `/api/companions/${host.id}/ignored`, { container: `unit:${u}`, ignored: on }, session.csrf);
      reload();
    } catch (e) { setError(e.message); }
  };
  const units = [...host.report.failed_units].sort((a, b) => ignored.includes(`unit:${a}`) - ignored.includes(`unit:${b}`));
  return (
    <Section title="failed systemd units"
      about={<>Services whose last run ended in an error; <code>systemctl status &lt;name&gt;</code> on the host says why, and
        <code> systemctl reset-failed &lt;name&gt;</code> clears one. <strong>Ignore</strong> sets one aside here without touching the host.</>}>
      <ul className="rows">{units.map((u) => {
        const off = ignored.includes(`unit:${u}`);
        return (
          <li key={u} className={off ? "muted" : undefined}>
            <span className={`dot dot-${off ? "unknown" : "down"}`} />
            <span className="row-main mono">{u}</span>
            <span className="row-note muted">{off ? "ignored" : ""}</span>
            <span className="row-act">{session?.signed_in && (
              <button className="btn btn-small" onClick={() => toggle(u, !off)}>{off ? "Stop ignoring" : "Ignore"}</button>
            )}</span>
          </li>
        );
      })}</ul>
      {error && <p className="form-error" role="alert">{error}</p>}
    </Section>
  );
}

// Each pending update and, for a security one, why: its advisories and CVEs,
// linked to the distro's own pages (design: the distro's sources only).
function PackageList({ packages, os, pending }) {
  const [all, setAll] = useState(false);
  const sec = packages.filter((p) => p.security);
  const shown = all ? packages : sec.length ? sec : packages.slice(0, 8);
  const more = packages.length - shown.length;
  return (
    <>
      <ul className="rows pkgs">{shown.map((p) => (
        <li key={p.name}>
          <span className="row-main"><span className="mono">{p.name}</span>
            {p.source && <span className="muted small"> from {p.source}</span>}
            <span className="row-sub muted mono">{p.from ? `${p.from} → ${p.to}` : `new: ${p.to}`}</span>
            {(p.advisories?.length > 0 || p.cves?.length > 0) && (
              <span className="row-sub why">{[...(p.advisories ?? []), ...(p.cves ?? [])].map((id) => {
                const url = advisoryUrl(id, os);
                return url ? <a key={id} className="mono" href={url} target="_blank" rel="noreferrer">{id}</a>
                  : <span key={id} className="mono">{id}</span>;
              })}</span>
            )}
            {p.security && !p.advisories?.length && !p.cves?.length && (
              <span className="row-sub muted small">{p.why === "none" ? "Shipped as a security update, but its changelog names no CVE or advisory."
                : p.why === "unavailable" ? "security update; its changelog could not be read yet, tried again each hour"
                : "security update; its advisory shows here within the hour"}</span>
            )}
            {p.changes && <span className="row-sub small pkg-changes">{p.changes}</span>}</span>
          <span className="row-note small">{p.security ? <span className={`tag tag-${p.severity === "Critical" || p.severity === "Important" ? "down" : "failing"}`}>
            {p.severity ? `${p.severity.toLowerCase()} security` : "security"}</span> : null}</span>
          <span className="row-act" />
        </li>
      ))}</ul>
      {(more > 0 || all) && (
        <button className="link-button small" onClick={() => setAll(!all)}>
          {all ? "Show only security updates" : `Show all ${pending ?? packages.length} updates`}</button>
      )}
    </>
  );
}

function Overview({ host, suggestions, session, reload, updateCommand, hosts }) {
  const r = host.report ?? {};
  const look = attention(host);
  return (
    <>
      {host.companion_stale && (
        <Section title="its companion is out of date">
          <p className="small">
            This host's companion has not updated itself to Argus's build for hours, so its fixes are not reaching this host.
            Reinstall it once; from then on it updates itself.
          </p>
          {session?.signed_in && <Reinstall host={host} hosts={hosts} session={session} updateCommand={updateCommand} onClose={() => {}} />}
        </Section>
      )}
      <Section title="needs a look">
        {look.length ? <Tags items={look} /> : <p className="muted small">Nothing. {hostSentence(host)}</p>}
        {r.reboot_required && (
          <p className="muted small">{host.patching?.plan?.enabled
            ? "Its next patch window reboots it. To do it sooner, press Patch now on the Patching tab."
            : "Give it a patch window on the Patching tab and Argus reboots it there."}</p>
        )}
        {r.docker_error && <p className="st-failing small">Docker: {r.docker_error}</p>}
        {r.updates?.error && <p className="st-failing small">Updates: {r.updates.error}</p>}
      </Section>
      {r.disks?.length > 0 && <Section title="disks"><Disks disks={r.disks} /></Section>}
      {(r.updates || r.reboot_required) && (
        <Section title="updates">
          <p className="small">
            {r.updates?.pending != null && (r.updates.pending
              ? `${r.updates.pending} waiting${r.updates.security != null ? `, ${r.updates.security} of them security` : ""} (${r.updates.manager}).`
              : `Up to date (${r.updates.manager}).`)}
            {r.reboot_required && <span className="st-failing"> Needs a reboot.</span>}
          </p>
          {r.updates?.packages?.length > 0 && <PackageList packages={r.updates.packages} os={r.os} pending={r.updates.pending} />}
        </Section>
      )}
      {r.failed_units?.length > 0 && <FailedUnits host={host} session={session} reload={reload} />}
      {host.checks.length > 0 && (
        <Section title="checks run from here">
          <ul className="rows">{host.checks.map((c) => (
            <li key={c.name}>
              <span className={`dot dot-${c.status === "unknown" ? "unknown" : c.status}`} />
              <span className="row-main"><a href={`#/check/${encodeURIComponent(c.name)}`}>{c.name}</a>
                <span className="row-sub muted mono">{c.kind} {c.target}</span></span>
              <span className={`row-note st-${c.status}`}>{c.status}</span>
            </li>
          ))}</ul>
        </Section>
      )}
      {suggestions?.length > 0 && <NotWatched suggestions={suggestions} session={session} reload={reload} />}
    </>
  );
}

function HostDetail({ host, tab, session, reload, refs, backups, hostNames, suggestions, updateCommand, hosts }) {
  const r = host.report ?? {};
  const [tone, word] = STATUS[host.status];
  const tabs = [["overview", "Overview"]];
  if (r.containers) tabs.push(["containers", `Containers (${r.containers.length})`]);
  if (r.proxmox) tabs.push(["guests", `Guests (${r.proxmox.guests.length})`]);
  tabs.push(["patching", "Patching"]);
  if (host.stacks?.length || host.adoptable?.length) tabs.push(["deploys", "Deploys"]);
  const current = tabs.some(([k]) => k === tab) ? tab : "overview";
  return (
    <article className="host-detail">
      <a className="back small muted" href="#/hosts">← Hosts</a>
      <header className="hd-head">
        <span className={`dot dot-${tone}`} />
        <h1>{host.name}</h1>
        <span className={`st-${tone}`}>{word}</span>
        {host.read_only && <span className="chip">monitoring only</span>}
        <span className="muted small hd-seen">{host.last_seen ? `reported ${agoText(host.age)}` : "waiting for its first report"}</span>
      </header>
      {host.report && <>
        <p className="muted hd-sub">{[r.os, host.address].filter(Boolean).join(" · ")}</p>
        {/* The details someone debugging wants, quieter than what the host is. */}
        <p className="muted hd-build">{[r.kernel && `kernel ${r.kernel}`, r.arch,
          r.version && `companion ${r.version.replace(/\+([0-9a-f]{7})[0-9a-f]*$/, "+$1")}`].filter(Boolean).join(" · ")}</p>
      </>}
      <HostActions host={host} session={session} reload={reload} hosts={hosts} updateCommand={updateCommand} />
      <nav className="subtabs hd-tabs">
        {tabs.map(([k, label]) => (
          <a key={k} href={`#/hosts?host=${encodeURIComponent(host.name)}&tab=${k}`} aria-current={k === current ? "page" : undefined}>{label}</a>
        ))}
      </nav>
      <div className="hd-body">
        {current === "overview" && <Overview host={host} suggestions={suggestions} session={session} reload={reload} updateCommand={updateCommand} hosts={hosts} />}
        {current === "containers" && (
          <Section title="containers" about="Ignore sets aside one you do not care about (a one-off that exited): it moves to the bottom and no longer counts as trouble. Stopped containers never page.">
            {r.containers.length ? <Containers list={r.containers} host={host} session={session} reload={reload} />
              : <p className="muted small">Docker runs, with no containers.</p>}
            {r.docker_error && <p className="st-failing small">Docker: {r.docker_error}</p>}
          </Section>
        )}
        {current === "guests" && <Guests host={host} hostNames={hostNames} session={session} reload={reload} />}
        {current === "patching" && (
          <Section title="patching">
            <Patching host={host} session={session} refs={refs} backups={backups} reload={reload} />
          </Section>
        )}
        {current === "deploys" && <>
          {host.stacks?.length > 0 && <Section title="stacks it runs"><Stacks stacks={host.stacks} session={session} reload={reload} /></Section>}
          <NotFromGit host={host} session={session} reload={reload} />
        </>}
      </div>
    </article>
  );
}

// The Patching view: every host's patching in one place. Guests share one plan,
// Proxmox servers each get their own day, and hosts without a backup Argus can
// see are listed with what to do.
function PatchPlan({ hosts, backups, session, reload }) {
  const [plan, setPlan] = useState({ day: 0, time: "03:00", level: "full", reboot: true, backup_days: 8 });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [error, setError] = useState(null);
  const servers = hosts.filter((h) => h.report?.proxmox);
  const guests = hosts.filter((h) => !h.report?.proxmox && h.patching?.suggested_backup?.startsWith("pve:"));
  const others = hosts.filter((h) => !servers.includes(h) && !guests.includes(h));
  const state = (h) => {
    const pt = h.patching;
    if (h.read_only) return ["unknown", "monitoring only"];
    if (pt.plan.elsewhere) return ["unknown", "patched by something else"];
    if (pt.plan.enabled) return pt.safety.ok ? ["up", `${DAYS[pt.plan.day]}s ${pt.plan.time}`] : ["failing", `on, but ${pt.safety.net}`];
    return ["unknown", "off"];
  };
  const backupOf = (h) => backups.find((b) => b.ref === h.patching.suggested_backup);
  const recent = (h) => { const b = backupOf(h); return b?.at && Date.now() / 1000 - b.at < plan.backup_days * 86400; };
  const ready = guests.filter((h) => !h.read_only && !h.patching.plan.enabled && !h.patching.plan.elsewhere && recent(h));
  const noBackup = guests.filter((h) => !h.patching.plan.enabled && !h.patching.plan.elsewhere && !recent(h));
  const put = (h, body) => send("PUT", `/api/patching/${h.name}`, {
    enabled: true, elsewhere: false, elsewhere_job: "", restore: h.patching.plan.restore || "", ...body }, session.csrf);
  const turnOn = async () => {
    setBusy(true); setError(null); setMsg(null);
    let n = 0;
    for (const h of ready) {
      try { await put(h, { ...plan, day: Number(plan.day), backup: h.patching.suggested_backup }); n++; }
      catch (e) { setError(`${h.name}: ${e.message}`); }
    }
    setMsg(`${n} guest${n === 1 ? "" : "s"} now patched on ${DAYS[plan.day]}s from ${plan.time}.`);
    setBusy(false);
    reload();
  };
  // The server Argus runs on goes last: if its update goes wrong, the other was fine.
  const argusHome = servers.find((s) => s.report.proxmox.guests.some((g) => guestHostName(g.name || "") === "argus"));
  const firsts = servers.filter((s) => s !== argusHome);
  const serverDay = (s) => (s === argusHome ? 3 : [1, 2, 4, 5][firsts.indexOf(s)] ?? 1);
  const set = (k) => (e) => setPlan({ ...plan, [k]: k === "day" || k === "backup_days" ? Number(e.target.value) : e.target.value });
  return (
    <div className="patch-plan">
      <Section title={`guests (${guests.length})`}
        about="Each guest is patched with all its updates and rebooted when an update needs it, one guest at a time, only when its Proxmox backup is recent. Change any one guest on its own page.">
        {ready.length === 0 ? (
          <p className="small muted">Every guest has a plan. A guest added later shows up here, ready to join it.</p>
        ) : <>
        <p className="small">One plan for every guest. Its backup is the one its Proxmox server makes, found for you.</p>
        <div className="form-grid">
          <label className="field"><span>Day</span>
            <select value={plan.day} onChange={set("day")}>{DAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}</select></label>
          <label className="field"><span>From (each host's clock)</span>
            <input type="time" value={plan.time} onChange={set("time")} required /></label>
          <label className="field"><span>Updates</span>
            <select value={plan.level} onChange={set("level")}><option value="full">All of them</option><option value="security">Security only</option></select></label>
          <label className="field"><span>Backup no older than (days)</span>
            <input type="number" min="1" max="366" value={plan.backup_days} onChange={set("backup_days")} /></label>
        </div>
        <label className="toggle"><input type="checkbox" checked={plan.reboot} onChange={(e) => setPlan({ ...plan, reboot: e.target.checked })} />
          <span>Reboot when an update needs it</span></label>
        {session?.signed_in && (
          <div className="actions">
            <button className="btn btn-primary" disabled={busy} onClick={turnOn}>
              {`Turn on for ${ready.length} guest${ready.length === 1 ? "" : "s"}`}</button>
            <span className="muted small">Guests that already have a plan keep it.</span>
          </div>
        )}
        </>}
        {noBackup.length > 0 && (
          <div className="no-backup">
            <p className="small st-failing">
              Not included, no backup from the last {plan.backup_days} days. Add them to a backup job in Proxmox
              (Datacenter, Backup) and they are ready after its next run, or, for one you could rebuild, patch it without a backup:
            </p>
            <ul className="rows">{noBackup.map((h) => (
              <li key={h.id}><span className="dot dot-failing" />
                <span className="row-main">{h.name}</span><span className="row-note" />
                <span className="row-act">{session?.signed_in && (
                  <button className="btn btn-small" disabled={busy}
                          onClick={async () => {
                            setError(null);
                            try { await put(h, { ...plan, day: Number(plan.day), backup: "", no_backup: true }); setMsg(`${h.name} is patched on ${DAYS[plan.day]}s without a backup.`); }
                            catch (e) { setError(`${h.name}: ${e.message}`); }
                            reload();
                          }}>Patch without a backup</button>
                )}</span></li>
            ))}</ul>
          </div>
        )}
        {msg && <p className="form-ok small" role="status">{msg}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <ul className="rows">{guests.map((h) => {
          const [tone, text] = state(h);
          const b = backupOf(h);
          return (
            <li key={h.id}><span className={`dot dot-${tone === "unknown" ? (recent(h) ? "hollow" : "failing") : tone}`} />
              <span className="row-main"><a className="quiet-link" href={`#/hosts?host=${encodeURIComponent(h.name)}&tab=patching`}>{h.name}</a>
                <span className="row-sub muted">{b?.at ? `backed up ${agoText(Date.now() / 1000 - b.at)} by ${b.server}`
                  : h.patching.plan.no_backup ? "patched without a backup, your choice" : `no backup on ${b?.server ?? "its server"}`}</span></span>
              <span className={`row-note ${tone === "up" ? "" : `st-${tone}`}`}>{text}</span><span className="row-act" /></li>
          );
        })}</ul>
      </Section>

      {servers.length > 0 && (
        <Section title={`proxmox servers (${servers.length})`}
          about="A server's update can need a reboot, and a reboot restarts every guest on it. So Argus patches a server only when every guest on it has a recent backup: if the server breaks, you reinstall Proxmox and restore them. It patches one server at a time, on its own day, and never while another server's last run failed. Proxmox keeps the previous kernel, so you can boot back into it. Major versions (9 to 10) stay manual.">
          {[...firsts, ...(argusHome ? [argusHome] : [])].map((s) => (
            <ServerPlan key={s.id} host={s} session={session} reload={reload} put={put}
                        defaultDay={serverDay(s)} last={s === argusHome} state={state(s)} />))}
        </Section>
      )}

      {others.length > 0 && (
        <Section title={`other hosts (${others.length})`}
          about="Argus cannot see a backup for these. If a VM snapshot or a backup job covers one, have it report to Argus as a timer or a run. Then link it on the host's Patching tab. Argus snapshots a host with a ZFS root itself before each run.">
          <ul className="rows">{others.map((h) => {
            const [tone, text] = state(h);
            return (
              <li key={h.id}><span className={`dot dot-${tone === "unknown" ? "hollow" : tone}`} />
                <span className="row-main">{h.name}<span className="row-sub muted">{h.patching?.safety?.net}</span></span>
                <span className={`row-note ${tone === "up" ? "" : `st-${tone}`}`}>{text}</span>
                <span className="row-act"><a className={`btn btn-small ${h.patching?.plan?.enabled ? "btn-ghost" : ""}`}
                  href={`#/hosts?host=${encodeURIComponent(h.name)}&tab=patching`}>{h.patching?.plan?.enabled ? "Change" : "Set up"}</a></span></li>
            );
          })}</ul>
        </Section>
      )}
    </div>
  );
}

function ServerPlan({ host, session, reload, put, defaultDay, last, state }) {
  const pt = host.patching.plan;
  const [day, setDay] = useState(pt.enabled ? pt.day : defaultDay);
  const [time, setTime] = useState(pt.enabled ? pt.time : "03:00");
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(false);
  const [tone, text] = state;
  const changed = !pt.enabled || Number(day) !== pt.day || time !== pt.time;
  const save = async (on) => {
    setError(null);
    try {
      await put(host, { enabled: on, day: Number(day), time, level: "full", reboot: true,
                        backup: "pve:guests", backup_days: pt.backup_days || 8 });
      setEditing(false);
      reload();
    } catch (e) { setError(e.message); }
  };
  const canEdit = session?.signed_in && !host.read_only;
  return (
    <div className="server-plan">
      <div className="server-plan-head">
        <a href={`#/hosts?host=${encodeURIComponent(host.name)}&tab=patching`}><strong>{host.name}</strong></a>
        <span className={`small ${tone === "up" ? "" : `st-${tone}`}`}>{text}</span>
        {last && <span className="muted small">patched last: Argus runs here</span>}
        {canEdit && pt.enabled && !editing && (
          <button className="link-button small server-plan-change" onClick={() => setEditing(true)}>Change</button>)}
      </div>
      <GuestsNet host={host} days={pt.backup_days || 8} session={session} reload={reload} />
      {canEdit && (editing || !pt.enabled) && (
        <div className="inline-form">
          <select value={day} onChange={(e) => setDay(Number(e.target.value))} aria-label="Day">
            {DAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}</select>
          <input type="time" value={time} onChange={(e) => setTime(e.target.value)} aria-label="Time" />
          <button className={`btn btn-small ${changed ? "btn-primary" : ""}`} disabled={!changed} onClick={() => save(true)}>
            {pt.enabled ? "Save" : "Turn on"}</button>
          {editing && <button className="btn btn-small" onClick={() => { setEditing(false); setDay(pt.day); setTime(pt.time); }}>Cancel</button>}
          {editing && <button className="btn btn-small btn-quiet-danger" onClick={() => save(false)}>Turn off</button>}
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

function useHostRoute() {
  const read = () => {
    const q = new URLSearchParams((window.location.hash.split("?")[1]) ?? "");
    return { name: q.get("host"), tab: q.get("tab"), add: q.get("add"), view: q.get("view") };
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

// A host just uninstalled: said for ten minutes, or until dismissed. Leave
// Argus keeps the week's list.
const FRESH = 600;
function Uninstalled({ list }) {
  const [gone, setGone] = useState(() => { try { return JSON.parse(localStorage.getItem("argus.uninstalledSeen") || "[]"); } catch { return []; } });
  const now = Date.now() / 1000;
  const show = list.filter((u) => now - u.at < FRESH && !gone.includes(`${u.name}-${u.at}`));
  const dismiss = (k) => {
    const next = [...gone, k].slice(-20);
    setGone(next);
    try { localStorage.setItem("argus.uninstalledSeen", JSON.stringify(next)); } catch { /* fine */ }
  };
  return show.map((u) => {
    const k = `${u.name}-${u.at}`;
    const log = u.log ? u.log.charAt(0).toUpperCase() + u.log.slice(1) : "";
    return (
      <p key={k} className="form-ok small" role="status">
        <strong>{u.name} is uninstalled</strong> ({agoText(now - u.at)}).{log && ` ${log}${/[.:]$/.test(log) ? "" : "."}`}{" "}
        <button className="link-button small" onClick={() => dismiss(k)}>Dismiss</button></p>
    );
  });
}

export default function Hosts({ data, session, reload, start, refs = [] }) {
  const [adding, setAdding] = useState(false);
  const route = useHostRoute();
  // #/hosts?add=<name>: "Add it by hand instead" on a guest Argus could not reach.
  useEffect(() => { if (route.add) setAdding(true); }, [route.add]);
  const hosts = data?.companions ?? [];
  const quiet = hosts.filter((h) => h.status === "quiet").length;
  const backups = data?.backups ?? [];
  const hostNames = new Set(hosts.flatMap((h) => [h.name, (h.report?.hostname ?? "").split(".")[0].toLowerCase()]).filter(Boolean));
  const selected = route.name && hosts.find((h) => h.name === route.name);
  if (selected) {
    return (
      <section className="hosts-page">
        <HostDetail host={selected} tab={route.tab} session={session} reload={reload} refs={refs}
                    backups={backups.filter((b) => b.server !== selected.name)} hostNames={hostNames}
                    suggestions={(start?.suggestions ?? []).filter((g) => g.host === selected.name)}
                    updateCommand={data?.update_command ?? ""} hosts={hosts} />
      </section>
    );
  }
  return (
    <section className="hosts-page">
      <div className="hosts-head">
        <div>
          <h1>Hosts</h1>
          <p className="lede muted">
            {hosts.length === 0 ? "No hosts yet. Add one to see its containers, disks and updates here."
              : `${hosts.length} host${hosts.length === 1 ? "" : "s"}${quiet ? `, ${quiet} gone quiet` : ""}. A host that misses three reports, or a disk past 95%, alerts you.`}
          </p>
        </div>
        {!adding && (session?.signed_in
          ? <button className="btn btn-primary" onClick={() => setAdding(true)}>Add a host</button>
          : <a className="btn" href="#/account">Sign in to add a host</a>)}
      </div>
      <Uninstalled list={data?.uninstalled ?? []} />
      {route.name && <p className="banner banner-warn">There is no host called {route.name} any more. <a href="#/hosts">All hosts</a></p>}
      {adding && session?.signed_in && (
        <AddHost key={route.add || ""} session={session} hosts={hosts} tlsPin={data?.tls_pin} initialName={route.add || ""}
                 onClose={() => { setAdding(false); if (route.add) window.location.hash = "/hosts"; reload(); }} />
      )}
      {data?.unknown?.length > 0 && (
        <p className="banner banner-warn">
          argus.yml has checks for {data.unknown.join(", ")}, which {data.unknown.length === 1 ? "is" : "are"} not
          added here yet. Those checks wait until {data.unknown.length === 1 ? "it reports" : "they report"}.
        </p>
      )}
      {hosts.length > 0 && (
        <nav className="subtabs hd-tabs">
          <a href="#/hosts" aria-current={route.view !== "patching" ? "page" : undefined}>All hosts</a>
          <a href="#/hosts?view=patching" aria-current={route.view === "patching" ? "page" : undefined}>Patching</a>
        </nav>
      )}
      {hosts.length > 0 && (route.view === "patching"
        ? <PatchPlan hosts={hosts} backups={backups} session={session} reload={reload} />
        : <HostList hosts={hosts} session={session} reload={reload} />)}
    </section>
  );
}
