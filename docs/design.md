# Argus design

_"v1" and "v2" below are generations of Argus built inside its author's homelab before it was released. The first public release is 1.0.0, from decision 21 on._

Status: approved by Devin 2026-10-30, with the five decisions below as suggested. M0 and M1 are under way.

## What v2 is

Argus v1 runs one homelab: it checks every service, shows each app's path from update PR to production, and records the patch runs. Everything around it (deploys, patching, paging) is this homelab's own tooling: Ansible, stack-sync, stack-bump, Prometheus and Alertmanager.

Argus v2 is the same idea as a product anyone can run: **install one container, add a companion to each host, connect GitHub, and merges to main deploy themselves while Argus watches and pages.** No Prometheus, Alertmanager or Grafana needed, and no config files to edit unless you want them.

The main value is the automated deploys of infrastructure and containers. Monitoring is how Argus proves a deploy worked.

### Hard requirements (Devin)

1. **Simple and intuitive.** Every choice a user has to make is in the UI, with sensible defaults. A first deploy takes minutes, not an afternoon.
2. **Fewest moving parts.** One server container, one companion binary. Nothing else to run.
3. **Alerts chosen in the UI.** One page decides who sends alerts: Argus (Telegram first) or Prometheus and Alertmanager. Switching is never a gap.
4. **Git and the UI together.** People who keep config in git (like this homelab) keep doing that; the UI edits the same config.
5. **Tokens, not trust in the LAN,** without making setup harder.
6. **Dogfooding.** This homelab moves onto v2 and the same companion everyone else gets.

### Not in v2

- Snapshots and rollback of whole VMs. These need hypervisor access (Proxmox today), and that is a different integration. This homelab keeps its Ansible patch runs, reporting to v2 as jobs, as it does to v1.
- Kubernetes, Nomad, Swarm. v2 deploys Docker Compose stacks.
- Metrics and graphs. Argus records states and events, not time series. Prometheus and Grafana remain the answer for graphs.
- Multiple users and roles. One admin account.
- Anything that listens on a host. Companions only make outbound connections.

## The pieces

```
                 GitHub (app repos, deploy repo, GHCR)
                          ▲  polls every minute; writes tag bumps
                          │  (one GitHub App, connected once)
                ┌─────────┴─────────┐
   browser ───► │      Argus        │ ──► Telegram (or /metrics for Prometheus)
   (login for   │  one container:   │
    settings)   │  API, UI, SQLite  │ ──► external dead-man's switch (optional)
                └─────────▲─────────┘
                          │  HTTPS, outbound from each host only:
                          │  "here is my state" / "what should run on me?"
             ┌────────────┼────────────┐
        companion     companion     companion      one per host, or one
        (host A)      (host B)      (Pi)           container next to Docker
```

### Argus (the server)

One container, as in v1: Python, FastAPI, SQLite, the React UI. v1's code is the starting point: checks, heartbeats, jobs, the GitHub poller and the Updates view all carry over.

New in v2:

- **Any number of sites.** v1 has exactly two (A and B). A site is just a name and an order.
- **A settings store.** Settings live in SQLite. Secrets (bot tokens, the GitHub App key, stack secrets) are encrypted with a key Argus generates on first start and keeps in its data volume, outside the database. They are shown masked and can only be replaced, never read back.
- **A login,** for settings only by default (see below).
- **A notifier** (Telegram first).
- **Companion management:** enrollment, tokens, desired state.
- **The deploy engine:** it decides what each host should run and bumps image tags (v1's stack-bump, moved inside Argus).

### The companion

One small Go program, a single static binary for amd64 and arm64. It is also published as a container image, for hosts where people prefer that. It is idle almost all the time: once a minute it reports and asks for its desired state.

It does three things:

1. **Reports state** that outside checks cannot see: each container's state and health, failed systemd units, a disk running out, pending updates, and whether a reboot is needed.
2. **Runs local checks:** HTTP, TCP and ping from inside the host, for services that Argus cannot reach.
3. **Deploys:** it fetches its desired state from Argus, and for each stack that changed runs `docker compose pull` and `docker compose up -d`, waits for the containers to be healthy, and reports the result.

Read-only mode (no deploys, no Docker writes) is a single switch at install, for people who only want monitoring.

## Setting up (what the user does)

1. **Start Argus:** one `docker run` or a compose snippet from the README. The first page asks for an admin password. A setup code printed in the container log is required to set it, so nobody else on the network can claim a fresh install.
2. **Alerts:** the Alerts page asks who sends alerts. For Telegram, paste a bot token and a chat ID and press **Send test**.
3. **Add a host:** click "Add a host", copy one command, and run it on the host. The host appears within a minute.
4. **Connect GitHub:** click "Connect GitHub", approve the app on the repos you choose, and pick the deploy repo.
5. Optional: **add checks** for anything with a URL or port.

## Companions in detail

### Enrollment

- "Add a host" creates an **enrollment code**: single use, valid for 15 minutes. The install command carries it, together with Argus's URL and Argus's certificate fingerprint.
- The companion makes its first call over HTTPS and **pins that fingerprint**. Argus can then use a self-signed certificate on a LAN with no man-in-the-middle risk, and with no certificate setup.
- The companion trades the code for its own **companion token** (256 random bits). Argus stores only a hash of it.
- Removing a host in the UI revokes its token at once.

### Desired state, signed

- Argus keeps an **ed25519 signing key**. The companion pins its public key at enrollment.
- Every desired-state response is signed, and a companion refuses anything that does not verify. A wrong TLS setup or a proxy in between cannot make a host run something else.
- **Companion policy, on the host:** a local file says what this host will never run, whatever it is told. By default that is privileged containers, host network, mounts outside an allowed list, and the Docker socket. Changing it needs someone on the host. This limits what a compromised Argus or a malicious commit to the deploy repo could do.

### Secrets for stacks

v1 keeps stack secrets age-encrypted in git and Ansible writes them to each host. In v2:

- Stack secrets are entered in the UI, or imported, and kept encrypted in Argus.
- They are sent only to the host that runs that stack, inside its signed desired state.
- The companion writes them to a root-only `.env` next to the compose file, as Ansible does today.
- Argus can decrypt them (the key is in its data volume), so its data volume and admin password guard every stack secret. An external secret store is a later, advanced option (decision 11).

### Private images

- Argus gives each companion a **short-lived, read-only pull credential** with its desired state. The companion uses it for that pull only and never stores it.
- The planned source is a GitHub App installation token with packages read. The first task of M4 is to prove GHCR accepts it.
- If it does not, the fallback is a long-lived read-only pull token kept in Argus and passed the same way. Hosts still hold no credentials of their own.

## GitHub

- **One connection, in Argus.** "Connect GitHub" uses GitHub's app-manifest flow: Argus creates its own GitHub App on the user's account in a couple of clicks, then the user installs it on the repos they choose. A fine-grained token is the fallback for people who cannot use an app.
- Permissions, kept to the minimum:
  - read: contents, pull requests, actions, Dependabot alerts, packages;
  - write: contents, on the deploy repo only.
- **Polling, not webhooks,** by default: every minute, so it works behind NAT with nothing exposed. A webhook URL is an optional speed-up for people whose Argus is reachable.
- The **pipeline** stays as in v1:
  1. Dependabot opens PRs; CI tests and scans them.
  2. Patch and minor updates merge on green; majors wait for the owner.
  3. main builds an image with a SHA tag.
  4. Argus bumps the tag in the deploy repo.
  5. The companion deploys it.
  6. Argus shows it healthy.

  v2 does not build images: that stays in the app's CI. v2 ships the reusable workflows (today's DevinMB/workflows) as a template.

## Config: git and the UI together

The config source is one of two, chosen at setup and changeable later:

- **Argus itself** (the default). UI edits are saved in the database. Export and import as YAML are always available.
- **A git repo,** as this homelab does today.
  - Argus reads the config from the repo every minute.
  - A UI edit becomes a commit to the repo, or a PR if the user prefers reviews.
  - A change made in git shows in the UI within a minute.
  - When an edit and a commit collide, the UI shows the newer git version and asks the user to redo the edit.

Secrets never go to git, in either mode.

## Alerts

The Alerts page asks one question: **who sends your alerts?**

- **Argus** (the default):
  - Telegram first; the channel is a small interface, so ntfy, email and webhooks fit later without a redesign.
  - A short list of what pages, with defaults: a critical check goes down or recovers; a job fails or is overdue; a companion or heartbeat goes quiet; a deploy fails.
  - Repeats while something stays down, at an interval the user picks.
- **Prometheus and Alertmanager:**
  - `/metrics` as today, with downloadable alert rules and an Alertmanager snippet.
  - **Handoff:** Argus keeps sending until the user confirms that Alertmanager's test alert arrived; only then does it turn its own off. The same in reverse.

### Who watches Argus

A stopped Argus cannot page about itself. The Alerts page offers two optional watchers:

- **An external dead-man's switch:** Argus pings a URL the user pastes (for example healthchecks.io's free tier) every minute, and that service alerts when the pings stop.
- **A peer Argus:** two Argus instances watch each other, as the two sites do here.

The page says plainly when neither is set up.

## Login

- One admin account. The password is set on first start, using the setup code from the log.
- Viewing is open by default, as in v1. One switch requires the login for everything.
- Passwords are hashed with argon2id. Sessions are HTTP-only, SameSite=Strict cookies, every write is CSRF-protected, and login attempts are rate-limited.
- HTTPS is on by default with a self-signed certificate; the user can bring their own or put Argus behind a proxy.

## Migration from v1 (this homelab)

| v1 piece | In v2 |
| --- | --- |
| Argus v1 | replaced by v2 at launch; repo renamed `argus-v1` and archived |
| `argus.yml` in homelab-ops | stays, with git as the config source |
| stack-sync on each Docker host | the companion's deploys |
| stack-bump on ops-runtime | the deploy engine inside Argus |
| mars-sync | stays as is (it runs MARS's own deploy script), reporting through its companion |
| Per-host deploy keys | removed; companions fetch from Argus |
| Prometheus, Alertmanager, Grafana | stay; Alerts page set to Prometheus |
| Ansible patch runs, ops-apply | stay, reporting as jobs |

The move happens host by host, with v1's stack-sync left in place until the companion on that host has deployed successfully.

## Milestones

Each milestone ships something usable and goes through PRs with tests, like v1.

| | Milestone | Done when |
| --- | --- | --- |
| M0 | `argus-next` repo, CI, v1 code imported, two sites generalized to many | v1's tests pass with any number of sites |
| M1 | **Standalone Argus:** login, settings store, encrypted secrets, Alerts page with Telegram, Send test, handoff to Prometheus, dead-man ping | a fresh install pages a phone with no Prometheus |
| M2 | **Companions, monitoring:** enrollment, tokens, state reports, local checks, read-only mode | a host added with one command shows its containers and disks |
| M3 | **Connect GitHub:** the App flow, the Updates view generalized | a fresh install shows its repos' pipeline |
| M4 | **Deploys:** desired state, signing, companion policy, stack secrets, pull credentials, tag bumps | a merge to main deploys to a companion with no other tooling |
| M5 | **Git-backed config:** reading the repo, UI edits as commits or PRs | this homelab's argus.yml is edited from the UI |
| M5b | **Getting started and patching:** a first-run checklist, check suggestions from a host's containers, adopting running stacks into the deploy repo, coverage per host; OS patching through companions with backup checks, pre-patch snapshots and optional restore tests (see below) | a fresh install gets its first host watched, patched on a schedule and deploying from git by following the checklist alone |
| M6 | **Launch:** README, install guide, example config, screenshots, license; repo swap; this homelab on v2 | someone else could install it from the README alone |
| after | **Security review and hardening** (see the roadmap) | findings fixed or documented |
| after | **External secret stores** (advanced, decision 11): HashiCorp Vault (and OpenBao), CyberArk | a stack secret is fetched from the store on its host, and Argus never holds its value |

M0 and M1 are the plan for Friday.

## Decisions (approved 2026-10-30)

Devin approved the design with each suggestion as written:

1. **License: Apache-2.0** (like MIT, plus a patent grant).
2. **HTTPS by default** with a self-signed certificate that companions pin. Bringing your own certificate or a proxy stays possible.
3. **Stack secrets kept in Argus,** sent only to the host that runs the stack, with import from age files for this homelab.
4. **Host patching through companions after launch;** v2 does deploys first.
5. **Viewing open by default** on the LAN, with one switch to require the login for everything.

## Decisions (approved 2026-10-30, later)

6. **A guided first run** (Devin asked for it): a "Get started" checklist replaces the empty home page, one next step at a time (alerts, first host, checks suggested from its containers, patching, GitHub, deploys from git with "adopt" for stacks already running). Afterwards each host card shows its own next steps and the home page a coverage line. No page is a dead end; nothing needs a config file.
7. **Host patching moves before launch** (replaces decision 4's "after launch"): a weekly window per host, one host at a time, a health check before the next. (Sunday default dropped by decision 12.)
8. **Patching goes only as far as the host's safety net allows** (replaced by decision 12):
   - none: security updates only, said plainly;
   - a recent backup that has not been restore-tested: security updates, and full upgrades and reboots only after the user accepts it;
   - a snapshot before patching (Proxmox guests, ZFS or btrfs roots) or a restore-tested backup: full upgrades, reboots, rollback.

   A backup location is not required. Argus checks backups (Proxmox task history; restic, borg and other tools report in as jobs) and never stores them. No fresh backup that week means no patch, and Argus says why.

   Two rules from a false page in this homelab (2026-11-02: a reused Proxmox VMID matched the removed guest's last archive, and a guest tagged `nobackup` paged every week):
   - Argus watches only the backups the owner links to a host. A host left out on purpose has nothing to go stale, so it never pages about backups.
   - Nothing about a removed host is inherited by a later one: removing a host forgets its plan, backup link, runs and requests, even if a new machine takes its name. When Argus reads Proxmox's backup list itself, it will match archives to a guest by more than its VMID (the guest's UUID, or archives newer than the guest), and honour the `nobackup` tag.
9. **Restore tests are optional and encouraged:** monthly, one guest or a sample of files at a time (Proxmox: restore to a temporary VM with no network, boot, check the guest agent, delete; restic or borg: their check plus a sampled restore compared by checksum). A failure pages. Hosts without one show it as a gap on their card and in the checklist.
10. **Argus's own data is backed up too:** a one-click encrypted export of settings and secrets, and a home-page warning until there is one.

## Decisions (approved 2026-11-02)

11. **Secrets: built in by default, an external store as an advanced option** (Devin). Stack secrets stay encrypted in Argus's database: nothing else to run, which suits most homelabs. After launch, for people who already run one, Argus integrates with HashiCorp Vault (and OpenBao, its open-source fork) and CyberArk. A secret is then a reference (`vault:kv/couchdb#password`) that the companion resolves on its host with its own credentials, so Argus never holds the value. Password rotation can build on that. Not chosen: GitHub Actions secrets (write-only; only a workflow can read them, which would mean deploying from Actions, against Argus's pull model), and password managers such as KeePass or LastPass (made for people, not servers).
12. **Patching: no backup, no patching, on any day the owner picks** (Devin, 2026-11-05). Replaces decision 8's levels and the built-in Sunday rule. Argus patches a host only when it can be brought back: a backup that reports to Argus and is more recent than the owner's limit for that host (default 8 days; 1 for nightly, 31 for monthly), or a ZFS root it snapshots first. With one, the run goes as far as the owner chose (all updates or security only, reboot or not). Without one, the host is not patched, and enabling it asks for a backup first; a backup gone stale skips the window and pages. Restore tests stay optional (decision 9): shown on the card, nudged after 35 days, never required. Why: if Argus breaks a machine, the owner must be able to get it back, and only the owner knows what recent enough means for them. Sunday was one homelab's backup day, not a rule for everyone. Not chosen: security updates on hosts without a backup (patched, but with no way back from a bad update).

## Decisions (2026-11-06)

13. **Build for the new user, and Argus sees Proxmox backups itself** (Devin, 2026-11-06: "set things up as if I was a new user and build around that premise always"). Most homelabs back up their guests with vzdump to a NAS, and without that showing in Argus decision 12 means nothing is ever patched. The companion on a Proxmox server reads guest configs, `storage.cfg` and each backup storage's `dump/` folder (files only, inside its read-only sandbox, in the background so a hung NFS mount cannot stop reports) and reports each guest's newest backup. A guest's card picks the backup whose guest has its name (`pve:<server>/<vmid>`), and the server's card lists its guests, their backups and which have Argus. A backup's notes must name the guest when they name anything, so a storage shared by two servers with the same VMIDs cannot vouch for the wrong guest. Not yet: Proxmox Backup Server storages, and patching the Proxmox server itself (vzdump backs up its guests, not the server; the rule for that is Devin's call).

14. **Add Argus to a guest from its Proxmox server** (Devin, 2026-11-06: "have part of the ui allow us to have proxmox companion add companions in each guest"). Running Add a host in eighteen guests one shell at a time was the clumsiest part of a fresh setup. Each guest row on a server's card has **Add Argus** (and **Add Argus to all** for the running ones): Argus makes a one-time code named after the guest, so it enrolls under its Proxmox name rather than a hostname like `localhost`, and the server's companion runs the install in it through `pct exec` (containers) or the QEMU guest agent (VMs that have it). The companion builds the command from checked fields only; codes and requests last an hour (the server adds guests one at a time), live in memory, and fail with a reason when unanswered. Off on monitoring-only servers and with `"no_guest_installs": true`.

15. **Patching from the user's side: one plan for the guests, a stricter rule for Proxmox servers** (Devin, 2026-11-06: "think about it from a users perspective... how are we patching the hosts? how are we patching the guests?"; he approved the server rule). Seventeen forms one host at a time was the wrong shape. **Hosts, Patching** turns on one plan for every guest whose Proxmox backup is recent, gives each server its own day, and lists hosts with no visible backup. A server's safety net is `pve:guests`: every guest on it backed up within the days set, guests marked as needing no backup excepted; one server at a time and never while another's newest run failed; the server Argus runs on is suggested last. Windows last six hours (they were three) because hosts take turns, and a host whose turn never comes pages as skipped instead of missing a week silently.

16. **Argus updates itself through the companion on its own machine, and the header shows its version** (Devin, 2026-11-07: "do both"; "I don't like the LIVE item... give the version running and if that version is behind maybe a small link to the update tooling"). Updating meant a console on CT 100 every time, and a new user would have to do the same. Argus learns what is newer from the product repo's newest green build on main and GitHub's compare (public, no connection needed). **Update Argus** asks the companion on the machine running Argus's image to pull and recreate that Compose project, with a three-minute health wait and a rollback to the previous image; the request is kept in settings because Argus restarts mid-way. Argus never gets the Docker socket. The header drops LIVE and the config hash for the version, when it last refreshed, and **Update available**.

17. **Deploy from git is a guided setup, starting from an empty repo** (Devin, 2026-11-07: "how does a user understand the deploy from git option easily and smoothly"; "lets do a fresh set up... if we have to make a new repo for compose thats fine"). The settings form assumed the user knew what a deploy repo, a `hosts/` file and two tokens were. `#/deploy` is four steps: a repo (Create one on GitHub, prefilled; an empty repo gets Argus's first commit through the contents API, since the git data API needs a parent), a write token for that repo (with the exact clicks), the running Compose projects moved in place in one batch (the adoption flow per project, reads requested together), and Dependabot in one press. Argus's own stack is left out: deploying itself would restart it mid-deploy, and it has Update Argus.

Decision 12, amended (Devin, 2026-11-07: "for things like this where I don't care if there's a backup. It still wont let me patch. Which I think is bad... I'd still like to avoid any vulns"). A backup stays the default safety net, but the owner may say a host **needs no backup** because it can be rebuilt: Argus then patches it, the page says "Safety net: none, by your choice", and every other rule (one host at a time, health checks, rollback of its containers, alerts) holds. An unpatched host is the larger risk.

## Decisions (2026-11-08)

18. **Letting go of Argus is a supported path, and coming back is too** (Devin, 2026-11-08: "how a user uninstalls Argus and companions... make sure they can safely let go of argus and reinstall if they want later"; on the choices: "1. yes 2. both, but the button lets have the user confirm by putting in their password... twice"). Removing a host only revoked its token: the program stayed, reporting into a wall, and the stacks Argus created lived in `/var/lib/argus-companion/stacks`, where deleting the companion's folder would take their compose files and `.env` secrets with it. Three journeys:
    - **Let one host go.** `argus-companion uninstall` hands every stack Argus created back as a plain Compose folder, `/opt/stacks/<name>` with its `.env` (`<name>-argus` when that exists), leaving running containers alone, then removes the service, the binary, `/etc/argus-companion` and `/var/lib/argus-companion`. Stacks Argus took over already live in their own folders and stay. On a host's page, **Uninstall** asks for the admin password twice; the next signed reply asks the companion to uninstall itself, it reports the handback, and only then does Argus forget the host. A host that is offline gets the command to run instead, and Remove (forget only) stays for a host that is gone.
    - **Let Argus go.** Settings, **Leave Argus**: export a backup, uninstall every companion (the same password twice), then a checklist of what lives outside Argus with a link for each: the GitHub App, the deploy repo's Dependabot file and the write token, the Telegram bot. Nothing outside Argus is deleted for the owner. Last, `docker compose down -v` for Argus itself.
    - **Come back.** A new Argus restores from the export (settings, secrets, hosts' records, certificate); each host runs Add a host again, and the stacks handed back in `/opt/stacks` are taken over in place: each handed-back folder holds a small `.argus-handed-back` file naming its stack, and a companion that has never deployed that stack deploys it there, not into a second copy in `/var/lib/argus-companion` (only inside the policy's `bind_roots`). Reinstalling a host never brings back its patch plan: it joins as a new host.
    Why the password twice: uninstalling software on another machine is the strongest thing the UI can do, so a borrowed or forgotten-open session is not enough. Not chosen: leaving handed-back stacks in `/var/lib/argus-companion/stacks` (a folder named after software that is gone).

19. **Hosts watch Argus, each with its own bot** (Devin, 2026-11-08: "A convenient way to watch and alert on Argus if it's down"; "I don't know if I like using outside software in the solution"; on the setup: "a command you run on the host that will watch Argus that adds the bot token and then that companion now serves as an alerter for Argus... I like that we only have one companion binary though"). A stopped Argus cannot say it stopped, and companions hold no Telegram token by design: one leaked host would leak the bot Argus alerts with, and Telegram tokens cannot be scoped.
    - **The watcher.** `sudo /usr/local/bin/argus-companion watch` on a host asks for a second bot's token (from @BotFather, not the one Argus sends with), waits for a message to that bot to learn the chat, sends a first message, and saves `/etc/argus-companion/watch.json` (root only). The token stays on that host: it never goes to Argus or travels in a reply, so even a compromised Argus cannot leak it. `--test` sends a test, `--off` stops watching; uninstalling the companion removes it with `/etc/argus-companion`. A monitoring-only host can watch: the bot is its owner's, not Argus's. Every command Argus prints names `/usr/local/bin/argus-companion` in full: sudo on Rocky and RHEL leaves `/usr/local/bin` out of its path (found on slayerz-lab, 2026-11-09).
    - **What it says.** After 5 minutes with no signed reply, "Argus is not answering", once, then a reminder every 6 hours; "Argus is answering again" with how long, when a reply comes. A 401 counts as an answer (Argus is up; it no longer knows the host). While Argus updates itself its replies carry a quiet window (15 minutes from the request), so Update Argus is not an outage. When Argus is the sender and its newest alert did not go out, the reply says why and the watcher passes it on once ("Argus is up but its last alert did not go out"), and again when Argus sends again.
    - **On the Alerts page.** Who watches Argus lists each watcher, its bot and when Telegram last answered; a watcher using Argus's own bot (the token's public bot ID matches) is flagged and does not count; a warning when every watcher runs on Argus's own machine. **Send a test** has every watcher send one on its next report. The other ways stay: name what already watches `/healthz` (Prometheus, Uptime Kuma), or a dead-man's switch ping URL; with Prometheus as the sender, its ArgusDown rule counts.
    - **In setup.** "Know if Argus stops" is a setup step, done once something watches Argus. Leave Argus names the watchdog bot and which hosts use it.
    Not chosen: a second Argus watching the first (a whole install to keep for one job); the token delivered by Argus to chosen hosts (Argus would hold it and could leak it); a daily "all is well" message (noticing a missing message is a human job).

20. **The app pipeline ships with Argus, as one pull request per repo** (Devin, 2026-11-09, after bouy-lab's Dependabot PRs failed on a stale cached upgrade layer in `DevinMB/workflows`: "Do other users not have automatic PR merging? I feel like that might be a core concept of Argus no?"; "lets handle this BEFORE v3"; "one-click I think that is better for the user"). The GitHub section above promised v2 would ship the reusable workflows as a template; only Devin's repos had them.
    - **One click, reviewed first.** A repo's details on Updates say what builds it: none, a shared workflow (a caller of an `app-ci.yml` in another repo, as DevinMB/workflows' are), Argus's (the file starts `# Made by Argus (pipeline N)`), or its own. For none or shared, **Add the pipeline** reads the repo's Dockerfiles (or the shared caller's settings, so image names stay) and shows the files and the images, with each image's name and test command editable. **Open the pull request** puts the files on `argus/pipeline` and opens the PR; Argus never merges it. A `ci.yml` Argus did not write is never replaced.
    - **Self-contained.** The workflow is written out for that repo: no `uses:` of another repo's workflow, only pinned actions that Dependabot then keeps current. Jobs per image (build, tests inside the image, healthcheck smoke, Trivy, push to GHCR as `sha-<7>`), then the Dependabot gate (patch and minor merge, majors labelled `waits-for-you`). `.github/dependabot.yml` is added only when missing: docker per image, pip, npm and gomod per manifest, github-actions.
    - **The cache fix.** Each image's build cache is scoped to its job and the ISO week, so an `apt-get upgrade` or `apk upgrade` layer rebuilds at least weekly and images no longer overwrite each other's cache.
    - **Permissions.** The GitHub App stays read-only until the first pull request: then Argus links to the app's permission page (Contents, Pull requests and Workflows to Read and write) and the owner accepts the change on GitHub. Each click asks GitHub for an installation token for that one repo with only those permissions, used once and never kept. A pasted token works if it has the same permissions.
    Not chosen: copy-paste instructions (more steps for every repo); asking for write in the app manifest from the start (write on every repo before it is needed); a reusable workflow in the Argus repo called by `uses:` (every user's CI would run code from Argus's repo at `main`).
21. **Version numbers, from 1.0.0** (Devin, 2026-11-09: "When we move to v3 finally at the end I think we need to start versioning our changes"; "I'd like the new repo to start on v1 versioning. This will be the first release to real people"). Every build said `2.0.0-dev` and Update Argus compared commits on main, so a user could not tell one Argus from another or choose when a change that needs them arrives.
    - **Tags make releases.** `release.yml` (run by hand with patch, minor or major, or an exact first version) tags main's head `v1.2.3` and starts that tag's build, which pushes `:1.2.3`, `:1.2` and `:1` and then publishes a GitHub release with notes generated from the merged pull requests. Main's green builds push only `sha-<7>` and `:edge`, as version `dev`.
    - **What each number means.** Patch: a fix or a dependency update; the Dependabot gate starts a patch release after each update it merges, so security fixes reach installs. Minor: something new. Major: something the owner must act on.
    - **What Argus offers.** A released Argus reads the repo's releases (public, no GitHub connection): newer releases of its own major are offered on Update Argus, listed with their pull request titles; a newer major is only announced, with a link to its notes, because `:1` never pulls a 2.x and moving means editing compose.yml. An image pinned to one release (`:1.2.3`) is told it will not move on a pull. Development builds keep comparing commits on main. Companions report `1.2.3+<commit>`, so the stale-companion check is unchanged.
    Not chosen: versions from pull request labels (one more thing to get right on every PR); a release per merge (Update Argus would nag on every docs change).
