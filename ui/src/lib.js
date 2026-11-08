// Pure helpers for the Argus UI. No DOM, so they are unit-tested (lib.test.js).

// Paused: its owner knows it is down; grey, never counted as a problem.
export const STATUSES = ["down", "failing", "unknown", "paused", "up"];
const RANK = Object.fromEntries(STATUSES.map((s, i) => [s, i]));

// Site display names come from argus.yml (via /api/info), never from the code.
export function siteName(sites, s) {
  return sites?.[s] ?? s;
}

// "across both sites", "across 4 sites"; nothing when there is only one.
export function acrossSites(n) {
  return n < 2 ? "" : n === 2 ? " across both sites" : ` across ${n} sites`;
}

export function summarize(checks) {
  const out = { total: checks.length, up: 0, failing: 0, down: 0, unknown: 0, paused: 0 };
  for (const c of checks) out[c.status in out ? c.status : "unknown"] += 1;
  return out;
}

// Worst first, then by name: what needs attention is always at the top.
export function byStatus(a, b) {
  return (RANK[a.status] ?? 2) - (RANK[b.status] ?? 2) || a.name.localeCompare(b.name);
}

// Groups in the order argus.yml lists them (the API keeps it); checks inside
// worst first.
export function groupChecks(checks) {
  const groups = new Map();
  for (const c of checks) {
    if (!groups.has(c.group)) groups.set(c.group, []);
    groups.get(c.group).push(c);
  }
  return [...groups.entries()].map(([group, list]) => [group, [...list].sort(byStatus)]);
}

// The ring: sites in their configured order, each by group (config order) and
// name, so each site is one unbroken arc and a tick never moves between refreshes.
export function ringOrder(checks, siteOrder = []) {
  const siteRank = Object.fromEntries(siteOrder.map((s, i) => [s, i]));
  const groupRank = new Map();
  for (const c of checks) if (!groupRank.has(c.group)) groupRank.set(c.group, groupRank.size);
  return [...checks].sort(
    (a, b) =>
      (siteRank[a.site] ?? siteOrder.length) - (siteRank[b.site] ?? siteOrder.length) ||
      groupRank.get(a.group) - groupRank.get(b.group) ||
      a.name.localeCompare(b.name),
  );
}

// Exposure is a tag, not a group: a target on a domain name is reached from
// the internet (through a Cloudflare tunnel); LAN services are addressed by IP.
export function isPublic(check) {
  if (check.kind !== "http") return false;
  try {
    const host = new URL(check.target).hostname;
    return !/^[\d.]+$/.test(host) && !host.includes(":") && host.includes(".");
  } catch {
    return false;
  }
}

// Everything a tile shows, as one lowercase string to search: name, target,
// group and each tag (kind, "pages", "public", "site B" and the site's name).
export function searchText(check, sites) {
  return [
    check.name, check.target, check.group, check.kind, check.severity,
    check.severity === "critical" && "pages alerts",
    isPublic(check) && "public",
    `site ${check.site}`, siteName(sites, check.site),
  ].filter(Boolean).join(" ").toLowerCase();
}

export function filterChecks(checks, { status = "all", site = "all", q = "", sites } = {}) {
  const needle = q.trim().toLowerCase();
  return checks.filter(
    (c) =>
      (status === "all" ||
        (status === "problems" ? c.status === "down" || c.status === "failing" : c.status === status)) &&
      (site === "all" || c.site === site) &&
      (!needle || searchText(c, sites).includes(needle)),
  );
}

// "12s", "4m", "3h", "2d". Null-safe; negative clock skew reads as "now".
export function ago(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return "never";
  const s = Math.max(0, Math.round(seconds));
  if (s < 5) return "now";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

// "a", "a and b", "a, b and c".
export function joinList(items) {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

// "just now" / "4m ago", for sentences.
export function agoText(seconds) {
  const a = ago(seconds);
  return a === "now" ? "just now" : a === "never" ? "never" : `${a} ago`;
}

export function ms(seconds) {
  if (seconds == null) return "–";
  return seconds < 1 ? `${Math.round(seconds * 1000)} ms` : `${seconds.toFixed(1)} s`;
}

export function stamp(ts) {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

// Never claim "all up" while checks are still waiting for a first result.
export function headline(s) {
  if (!s.total) return "Nothing watched yet.";
  if (s.down) return "Something is down.";
  if (s.failing) return "Something is wobbling.";
  if (s.unknown) return "Starting up.";
  return "All quiet.";
}

export function siteNote(s) {
  if (s.down) return `${s.down} down`;
  if (s.failing) return `${s.failing} failing`;
  if (s.unknown) return `${s.unknown} waiting`;
  if (s.paused) return `${s.paused} paused`;
  return "all up";
}

// ---- the app pipeline (Updates view, tracks) ----

export const GATE = "github-actions[bot]"; // the Dependabot merge gate merges as this
// Merged by a bot: the gate, or Dependabot's own auto-merge.
export const isAuto = (by) => by === GATE || by === "dependabot[bot]";
// Links in API data are drawn only when they point at the web over https.
export function safeHref(url) {
  return typeof url === "string" && /^https:\/\//.test(url) ? url : undefined;
}

// Jobs (decision 0017): runs that systemd starts and that report back here.
export const JOB_TONE = { ok: "up", failed: "down", late: "down", never: "failing" };

// "12m", "1h 4m": how long a run took.
export function took(started, finished) {
  if (started == null || finished == null) return "–";
  const s = Math.max(0, Math.round(finished - started));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

// What a job's cell says under its name.
export function jobLine(job, now) {
  if (!job.last) return job.schedule ? `first run ${job.schedule}` : "no run reported yet";
  const when = agoText(now - job.last.finished);
  if (job.status === "late") return `last ran ${when}, past its ${ago(job.max_age)} limit`;
  return `${job.last.result === "ok" ? "passed" : "failed"} ${when}`;
}

// ---- life cycle: where each app is, in words (Updates view) ----

// The board's columns, in the order a change travels.
export const STAGES = [
  ["testing", "Testing", "An update PR is open and CI is checking it"],
  ["merging", "Merging", "Green PRs merge themselves; majors wait for you"],
  ["building", "Building", "main builds an image and Trivy scans it"],
  ["deploying", "Deploying", "The new tag goes out to its host"],
  ["live", "Live", "Running the newest change"],
];

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// One app's place in the life cycle: the column it sits in, a tone, a short
// label and one sentence. What needs a person outranks what is moving, which
// outranks "up to date". `you` marks the ones that need the owner.
export function lifecycle(repo) {
  const steps = repo.track ?? [];
  const step = (k) => steps.find((s) => s.key === k);
  const ups = repo.updates ?? [];
  const red = ups.filter((u) => !u.waits_for_you && u.ci === "failure");
  const major = ups.filter((u) => u.waits_for_you);
  const green = ups.filter((u) => !u.waits_for_you && u.ci === "success");
  const testing = ups.filter((u) => !u.waits_for_you && (u.ci === "running" || u.ci === "none"));
  const more = (list) => (list.length > 1 ? ` (and ${list.length - 1} more)` : "");
  const deployedAt = steps.find((s) => s.key === "deployed" && s.status === "done")?.at ?? null;
  const out = (column, tone, label, sentence, url, you = false, where = "") =>
    ({ column, tone, label, sentence, url, you, deployedAt, where });

  const broken = steps.find((s) => s.status === "failed" && s.key !== "updates");
  if (broken) {
    const [column, label] = {
      built: ["building", "Build failed"],
      bumped: ["deploying", "Deploy stuck"],
      deployed: ["deploying", "Deploy failed"],
      healthy: ["live", "Down"],
    }[broken.key] ?? ["live", broken.title];
    return out(column, "down", label, broken.detail, broken.url, true);
  }
  const updates = step("updates");
  if (updates?.status === "failed" && !red.length) {
    return out("testing", "down", "Dependabot failing",
      `${updates.detail}. New versions are not being proposed until it is fixed.`, updates.url, true);
  }
  if (red.length) {
    return out("testing", "down", "Update failing CI",
      `${red[0].title} failed its checks, so it will not merge on its own${more(red)}.`, red[0].url, true);
  }
  if (major.length) {
    return out("merging", "failing", "Waits for you",
      `${major[0].title} is a major version: review it and merge${more(major)}.`, major[0].url, true);
  }
  const moving = steps.find((s) => s.status === "running" && s.key !== "updates");
  if (moving) {
    const [column, label] = {
      built: ["building", "Building"],
      bumped: ["deploying", "Deploying"],
      deployed: ["deploying", "Deploying"],
      healthy: ["live", "Checking health"],
    }[moving.key] ?? ["live", moving.title];
    // "Building 81a4f0c" says little: name the change being built.
    const detail = moving.key === "built" && repo.main?.title
      ? `Building: ${tidyTitle(repo.main.title)}` : moving.detail;
    return out(column, "accent", label, detail, moving.url);
  }
  if (testing.length) {
    return out("testing", "accent", "Testing update",
      `${testing[0].title}${more(testing)}: CI is running; it merges itself when green.`, testing[0].url);
  }
  if (green.length) {
    return out("merging", "accent", "Merging",
      `${green[0].title}${more(green)} passed CI; the gate merges it within minutes.`, green[0].url);
  }

  const main = repo.main;
  if (!main) {
    return out("live", "unknown", "Not tracked", `No CI runs on ${repo.branch ?? "main"}, so Argus cannot follow its changes.`);
  }
  const deployed = step("deployed");
  const healthy = step("healthy");
  const change = tidyTitle(main.title) || `sha-${main.sha}`;
  if (!deployed) {
    return repo.configured === false
      ? out("live", "up", "Up to date", `Built ${change}. Deployed outside Argus, so it is not followed further.`,
        undefined, false, "built")
      : out("live", "up", "Up to date", `Built ${change}. It ships inside another app, so it has no deploy of its own.`,
        undefined, false, "ships in another app");
  }
  if (deployed.status === "not_wired") {
    return out("live", "unknown", "Built, not deployed", `${change} is built; ${deployed.detail.toLowerCase()}.`);
  }
  const where = repo.stack?.host ? ` on ${repo.stack.host}` : "";
  if (healthy?.status === "paused") {
    return out("live", "paused", "Paused", `Live${where}. ${healthy.detail}, so it does not page.`);
  }
  const check = healthy?.status === "not_wired" ? " (no health check)" : "";
  return out("live", "up", "Up to date", `Live${where}${check}. Last change: ${change}.`, undefined, false,
    `live${where}${check}`);
}

// An HTTP failure in words: what happened and what to do, with the code kept for
// anyone who needs it (docs/VOICE.md: never only an HTTP code).
export function httpText(status) {
  if (status === 401) return "You are signed out (HTTP 401). Sign in and try again.";
  if (status === 403) return "Argus refused this (HTTP 403). Reload the page, sign in again and retry.";
  if (status === 404) return "Argus does not have this (HTTP 404). It may be on an older build: reload the page.";
  if (status >= 502 && status <= 504) return `Argus did not answer (HTTP ${status}). It may be restarting: try again in a minute.`;
  if (status >= 500) return `Argus hit an error (HTTP ${status}). Its log has the details: docker logs argus.`;
  return `The request failed (HTTP ${status}).`;
}

// GitHub's default merge titles say little ("Merge pull request #14 from
// owner/dependabot/pip/updates-2e49036dea"): say what they were.
export function tidyTitle(title = "") {
  const m = title.match(/^Merge pull request #(\d+) from \S+?\/(\S+)/);
  if (!m) return title;
  return m[2].startsWith("dependabot/") ? `a Dependabot update (#${m[1]})` : `pull request #${m[1]}`;
}

// The page's one-sentence answer: does anything need you, is anything moving.
export function lifecycleHeadline(states) {
  const you = states.filter((s) => s.you).length;
  const moving = states.filter((s) => !s.you && s.tone === "accent").length;
  if (you) return `${plural(you, "app needs", "apps need")} you.`;
  if (moving) return `${plural(moving, "update")} on the way, nothing needs you.`;
  return `All ${plural(states.length, "app")} up to date.`;
}

// The next time a weekday's clock time comes round (day 0 Sunday .. 6 Saturday),
// in the viewer's time zone, which for this homelab is the runs' own.
export function nextWeekday(day, now, hhmm = "00:00") {
  const [h, m] = hhmm.split(":").map(Number);
  const d = new Date(now * 1000);
  d.setDate(d.getDate() + ((day - d.getDay() + 7) % 7));
  d.setHours(h, m, 0, 0);
  if (d.getTime() <= now * 1000) d.setDate(d.getDate() + 7);
  return d;
}

export function dayText(d) {
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

// "Mondays 03:00" -> {day: 1, time: "03:00"}. Null for any other shape.
const DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
export function scheduleDay(schedule) {
  const m = /^([a-z]+?)s?\s+(\d\d:\d\d)$/.exec(schedule?.trim().toLowerCase() ?? "");
  const day = m ? DAYS.indexOf(m[1]) : -1;
  return day < 0 ? null : { day, time: m[2] };
}

// "Mondays 03:00" -> "Mon, Nov 2, 03:00", the next run. Null for any other shape.
export function nextRun(schedule, now) {
  const d = scheduleDay(schedule);
  return d && `${dayText(nextWeekday(d.day, now, d.time))}, ${d.time}`;
}

// A job's last run in one sentence anyone can read.
export function jobResult(job, now) {
  const last = job.last;
  if (!last) return job.schedule ? `Has not run yet. First run: ${job.schedule}.` : "Has not run yet.";
  const items = last.items ?? [];
  const n = (r) => items.filter((i) => i.result === r).length;
  let what = last.summary;
  if (items.length) {
    const parts = [`${n("ok")} of ${items.length} done`];
    if (n("failed")) parts.push(`${n("failed")} failed`);
    if (n("skipped")) parts.push(`${n("skipped")} skipped`);
    what = parts.join(", ");
  }
  const verdict = last.result === "ok" ? "Passed" : "Failed";
  const late = job.status === "late" ? `, and is overdue (limit ${ago(job.max_age)})` : "";
  return `${verdict} ${agoText(now - last.finished)}${late}, took ${took(last.started, last.finished)}${what ? `: ${what}` : ""}.`;
}

// Merged PRs across every repo, newest first: the page's "what happened" list.
export function recentMerges(repos = [], limit = 8) {
  return repos
    .flatMap((r) => r.merged.map((m) => ({ ...m, repo: r.name })))
    .sort((a, b) => b.merged_at - a.merged_at)
    .slice(0, limit);
}

// --- settings and alerts ---

// What the API's validation errors mean, per field, in words.
const FIELD_HELP = {
  telegram_token: "That does not look like a bot token. BotFather gives one like 123456789:AAE...",
  chat_id: "A chat ID is a number (negative for groups and channels) or @channelname.",
  link: "Use the address you open Argus at, starting with https:// or http://.",
  watch_url: "Paste the full ping URL, starting with https://.",
  password: "Use at least 10 characters.",
  new: "Use at least 10 characters.",
};

export function errorText(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return [...new Set(detail.map((e) => FIELD_HELP[e.loc?.at(-1)] ?? e.msg))].join(" ");
  }
  return String(detail);
}

// One sentence on how paging works, for the Checks page.
export function pagingSentence(alerts, critical) {
  const marked = `${critical} ${critical === 1 ? "check is" : "checks are"} marked pages`;
  if (!alerts) return `${marked}.`;
  if (alerts.sender === "prometheus") {
    return `${marked}: if one goes down, Prometheus and Alertmanager send it on. The rest show here only.`;
  }
  if (!alerts.ready) {
    return `${marked}, but nothing sends alerts yet: set up Telegram on the Alerts page.`;
  }
  return `${marked}: if one goes down, Argus sends it to Telegram. The rest show here only.`;
}

export function watcherSentence(alerts) {
  switch (alerts?.watched) {
    case "hosts":
      return "Hosts watch Argus and message you through their own bot if it stops answering.";
    case "url":
      return "An outside service expects a ping from Argus every minute, and alerts you if the pings stop.";
    case "elsewhere":
      return "Something outside Argus watches it, by your word on the Alerts page.";
    case "prometheus":
      return "Prometheus alerts you if it cannot scrape Argus (the ArgusDown rule).";
    default:
      return "Nothing watches Argus itself yet. If it stopped, nobody would tell you: add a watcher on the Alerts page.";
  }
}

export const REPEATS = [[0, "Never"], [60, "Every hour"], [240, "Every 4 hours"],
  [720, "Every 12 hours"], [1440, "Once a day"]];

// ---- hosts (companions) ----

// "512 MB", "1.8 GB", "3.6 TB".
export function bytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return i === 0 ? `${n} B` : `${n < 10 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

export const DISK_WARN = 0.85;
export const DISK_FULL = 0.95; // pages, as on the server

export function diskTone(d) {
  const r = d.total ? d.used / d.total : 0;
  return r >= DISK_FULL ? "down" : r >= DISK_WARN ? "failing" : "up";
}

// A container with a problem: stopped, restarting or failing its health check.
export function containerTrouble(c) {
  return c.state !== "running" || c.health === "unhealthy";
}

// Problems first, ignored ones last, then by name.
export function sortContainers(list = [], ignored = []) {
  const rank = (c) => (ignored.includes(c.name) ? 2 : containerTrouble(c) ? 0 : 1);
  return [...list].sort((a, b) => (rank(a) - rank(b)) || a.name.localeCompare(b.name));
}

// One plain sentence per host: what is running and what needs a look.
export function hostSentence(host) {
  if (host.status === "waiting") return "Enrolled; waiting for its first report.";
  const r = host.report ?? {};
  const parts = [];
  if (r.containers) {
    const ignored = host.ignored ?? [];
    const bad = r.containers.filter((c) => !ignored.includes(c.name) && containerTrouble(c)).length;
    const n = r.containers.length;
    parts.push(n === 0 ? "no containers"
      : bad ? `${n} container${n === 1 ? "" : "s"}, ${bad} not running well`
      : `${n} container${n === 1 ? "" : "s"}, all running`);
  }
  const disks = r.disks ?? [];
  if (disks.length) {
    const fullest = Math.max(...disks.map((d) => (d.total ? d.used / d.total : 0)));
    parts.push(`${disks.length === 1 ? "disk" : `${disks.length} disks, fullest`} ${Math.round(fullest * 100)}% used`);
  }
  const units = failedUnits(host);
  if (units.length) parts.push(`${units.length} failed systemd unit${units.length === 1 ? "" : "s"}`);
  const p = r.updates?.pending;
  if (p) parts.push(`${p} update${p === 1 ? "" : "s"} waiting${r.updates.security ? ` (${r.updates.security} security)` : ""}`);
  if (r.reboot_required) parts.push("needs a reboot");
  const text = parts.length ? parts.join(", ") : "reporting";
  return text[0].toUpperCase() + text.slice(1) + ".";
}

// "VM 110 deals-lab on butts-lab, backed up 2d ago"
export function backupLabel(b, now = Date.now() / 1000) {
  const what = `${b.type === "lxc" ? "CT" : "VM"} ${b.vmid}${b.name ? ` ${b.name}` : ""} on ${b.server}`;
  return `${what}, ${b.at ? `backed up ${agoText(now - b.at)}` : "no backup yet"}`;
}

// A guest's name as Argus names its host (as companions.name_from does).
export function guestHostName(name) {
  return name.split(".")[0].toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
}

// What on a host needs a look, worst first, as short tags: [tone, text].
export function attention(host) {
  if (host.status === "waiting") return [["unknown", "waiting for its first report"]];
  const r = host.report ?? {};
  const out = [];
  if (host.status === "quiet") out.push(["down", `quiet ${agoText(host.age)}`]);
  if (host.companion_stale) out.push(["failing", "companion out of date"]);
  const ignored = host.ignored ?? [];
  const bad = (r.containers ?? []).filter((c) => !ignored.includes(c.name) && containerTrouble(c)).length;
  if (bad) out.push(["down", `${bad} container${bad === 1 ? "" : "s"} down`]);
  const units = failedUnits(host);
  if (units.length) out.push(["down", `${units.length} failed unit${units.length === 1 ? "" : "s"}`]);
  const last = host.patching?.runs?.[0];
  if (last?.result === "failed") out.push(["down", "last patch run failed"]);
  for (const d of r.disks ?? []) {
    const tone = diskTone(d);
    if (tone !== "up") out.push([tone, `${d.mount} ${Math.round((d.used / d.total) * 100)}% full`]);
  }
  const u = r.updates;
  if (u?.error) out.push(["failing", "updates unreadable"]);
  else if (u?.pending) out.push([u.security ? "failing" : "unknown", `${u.pending} update${u.pending === 1 ? "" : "s"}${u.security ? `, ${u.security} security` : ""}`]);
  if (r.reboot_required) out.push(["failing", "needs a reboot"]);
  return out;
}

const SHORT_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// The patch column: "Tue 03:00", "elsewhere" or "not set".
export function patchLabel(host) {
  const p = host.patching?.plan;
  if (host.read_only) return ["unknown", "monitoring only"];
  if (!p) return ["unknown", "–"];
  if (p.elsewhere) return ["unknown", "by something else"];
  if (p.enabled) return ["up", `${SHORT_DAYS[p.day]} ${p.time}`];
  return ["unknown", "not set"];
}

// Hosts as a tree: each Proxmox server with its guests (a guest that has Argus
// is its own host, shown under its server), then the other hosts.
export function hostTree(hosts) {
  const byName = new Map(hosts.map((h) => [h.name, h]));
  const placed = new Set();
  const servers = hosts.filter((h) => h.report?.proxmox);
  const rows = [];
  for (const s of servers) {
    placed.add(s.name);
    const guests = s.report.proxmox.guests.map((g) => {
      const h = byName.get(guestHostName(g.name || ""));
      const host = h && !h.report?.proxmox && !placed.has(h.name) ? h : null;
      if (host) placed.add(host.name);
      return { guest: g, host };
    });
    rows.push({ host: s, guests });
  }
  for (const h of hosts) if (!placed.has(h.name)) rows.push({ host: h, guests: [] });
  return rows;
}

// A short line under a host's name: what it is and where.
export function hostSub(host, guest) {
  const r = host?.report ?? {};
  const os = r.proxmox ? "Proxmox VE" : (r.os || "").replace(/ GNU\/Linux/, "").replace(/\s*\(.*\)$/, "");
  return [guest && `${guest.type === "lxc" ? "CT" : "VM"} ${guest.vmid}`, os, host?.address].filter(Boolean).join(" · ");
}

// Why adding Argus to a guest failed, in plain words, and what to do next.
// `manual` says whether running Add a host in the guest is the way out.
export function installHelp(output = "", guest = {}, origin = globalThis.location?.origin ?? "Argus") {
  const o = output.toLowerCase();
  const vm = guest.type === "qemu";
  if (o.includes("install: not found") || o.includes("home assistant os")) {
    return { title: "This guest is an appliance", manual: false, steps: [
      "Appliances such as Home Assistant OS have no standard tools and update themselves, so they cannot run a companion.",
      "Press Skip: it stops counting, and Add Argus to all leaves it out."] };
  }
  if (vm && (o.includes("guest agent") || o.includes("qemu-guest-agent"))) {
    return { title: "The QEMU guest agent is not running in this VM", manual: true,
      lead: "Argus reaches a VM through its guest agent. Add Argus by hand (below), or turn the agent on:", steps: [
      "In the VM, install and start it. Debian, Ubuntu: `sudo apt install -y qemu-guest-agent && sudo systemctl enable --now qemu-guest-agent` Rocky, Fedora: `sudo dnf install -y qemu-guest-agent && sudo systemctl enable --now qemu-guest-agent`",
      "In Proxmox: the VM's Options, QEMU Guest Agent, Enabled.",
      "Shut the VM down and start it again (a reboot from inside is not enough), then Try again."] };
  }
  if (o.includes("not running") && !o.includes("agent")) {
    return { title: "The guest is not running", manual: false, steps: ["Start it in Proxmox, then Try again; or Skip it if it stays off."] };
  }
  if (/curl: \((6|7|28)\)/.test(o) || o.includes("could not resolve") || o.includes("timed out") || o.includes("connection refused")) {
    return { title: "The guest could not reach Argus", manual: true, steps: [
      `It tried ${origin}. Check the guest's network (gateway, DNS) and any firewall between it and Argus, then Try again.`] };
  }
  if (o.includes("pinnedpubkey") || o.includes("curl: (90)") || o.includes("curl: (60)")) {
    return { title: "Argus's certificate did not match", manual: false, steps: [
      "Something between the guest and Argus answered with another certificate (a proxy?). Make sure the guest reaches Argus directly, then Try again."] };
  }
  if (o.includes("code") && (o.includes("expired") || o.includes("used") || o.includes("401") || o.includes("403"))) {
    return { title: "The one-time code ran out", manual: false, steps: ["Press Try again: it makes a new code."] };
  }
  if (o.includes("never answered")) {
    return { title: "The Proxmox server never picked it up", manual: true, steps: [
      "The server's companion may be out of date or not reporting. Check the server's row, then Try again."] };
  }
  return { title: "Adding Argus failed", manual: true, steps: ["The output below says what went wrong. Try again, or add Argus by hand."] };
}

// A Proxmox server is patched only when every guest on it has a recent backup.
// Guests left out of backups on purpose (a model cache, say) can be marked so.
export function guestsNet(host, days, now = Date.now() / 1000) {
  const ignored = host.ignored ?? [];
  return (host.report?.proxmox?.guests ?? []).map((g) => {
    const age = g.backup ? now - g.backup.at : null;
    const exempt = ignored.includes(`nobackup:${g.vmid}`);
    return { ...g, age, exempt, ok: exempt || (age !== null && age < days * 86400) };
  });
}


// Failed systemd units the owner has not set aside (Ignore on the host page).
export function failedUnits(host) {
  const ignored = host.ignored ?? [];
  return (host.report?.failed_units ?? []).filter((u) => !ignored.includes(`unit:${u}`));
}

// ---- where a check runs, and checks of one port ----

const LOOPBACK = new Set(["127.0.0.1", "localhost", "::1"]);
const isIP = (h) => /^[0-9.]+$/.test(h) || h.includes(":");

// The host and port a check knocks on (as the server's config.endpoint).
export function endpoint(c) {
  let host, port;
  if (c.kind === "tcp") {
    const i = c.target.lastIndexOf(":");
    host = c.target.slice(0, i).replace(/^\[|\]$/g, ""); port = Number(c.target.slice(i + 1));
  } else if (c.kind === "http") {
    try {
      const u = new URL(c.target);
      host = u.hostname.replace(/^\[|\]$/g, ""); port = Number(u.port) || (u.protocol === "https:" ? 443 : 80);
    } catch { return null; }
  } else return null;
  if (c.companion && LOOPBACK.has(host)) host = `companion:${c.companion}`;
  return `${host}:${port}`;
}

// Where a check runs: the host it probes, by its companion or its address;
// "Websites" for a public name; "Other devices" for an address with no companion.
export function placeOf(c, hosts = []) {
  if (c.companion) return c.companion;
  let host = c.target;
  if (c.kind === "http") { try { host = new URL(c.target).hostname; } catch { /* keep */ } }
  else if (c.kind === "tcp") host = c.target.slice(0, c.target.lastIndexOf(":"));
  host = host.replace(/^\[|\]$/g, "");
  const h = hosts.find((x) => x.address === host || x.name === host);
  if (h) return h.name;
  return isIP(host) ? "Other devices" : "Websites";
}

// Checks that knock on the same port, each set with the one to keep first:
// the one that alerts you, then HTTP (it proves the app answers) over TCP,
// then the one with a path.
export function duplicates(checks) {
  const by = {};
  for (const c of checks) {
    const e = endpoint(c);
    if (e) (by[e] ??= []).push(c);
  }
  const score = (c) => (c.severity === "critical" ? 4 : 0) + (c.kind === "http" ? 2 : 0)
    + (c.kind === "http" && new URL(c.target).pathname.length > 1 ? 1 : 0);
  return Object.values(by).filter((l) => l.length > 1)
    .map((l) => [...l].sort((a, b) => score(b) - score(a) || a.name.localeCompare(b.name)));
}

// Each host's machine: a guest's Proxmox server, else the host itself, as on
// the Hosts page. Checks grouped by host sit under these.
export function machineOf(hosts = []) {
  const out = {};
  for (const { host, guests } of hostTree(hosts)) {
    out[host.name] = host.name;
    for (const g of guests) if (g.host) out[g.host.name] = host.name;
  }
  return out;
}

// A security advisory or CVE on its distro's own page.
export function advisoryUrl(id, os = "") {
  const ubuntu = /ubuntu/i.test(os);
  if (/^RLSA-/.test(id)) return `https://errata.rockylinux.org/${id}`;
  if (/^USN-/.test(id)) return `https://ubuntu.com/security/notices/${id}`;
  if (/^(DSA|DLA)-/.test(id)) return `https://security-tracker.debian.org/tracker/${id}`;
  if (/^CVE-/.test(id)) {
    if (ubuntu) return `https://ubuntu.com/security/${id}`;
    if (/debian/i.test(os)) return `https://security-tracker.debian.org/tracker/${id}`;
    return `https://www.cve.org/CVERecord?id=${id}`;
  }
  return null;
}
