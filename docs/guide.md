# Using Argus

What each part of Argus does, and how it keeps you safe while it does it. Installing: [install.md](install.md). Every config field: [config.md](config.md).

## Getting started

A new install shows a short checklist on its home page: make sure alerts reach you, add a host, watch what matters, connect GitHub, deploy from git. One step is open at a time, with a line on why it matters and one button. Once a host reports, Argus suggests a check for each port its containers publish ("jellyfin :8096 on nas"); **Watch** fills in the form. Afterwards a coverage line ("12 of 15 published services watched · 4 of 4 hosts reporting") and each host's "not watched yet" list show what to do next. The checklist can be hidden.

## Pausing a check

When you already know why something is down (a server switched off until another project is done), open the check and press **Pause…**. Add a reason if you like, and choose when the pause ends:

- **Until it is back up** (the default): it resumes by itself once it has been down and answers again.
- **Until I resume it**: it stays paused even when it answers.

A paused check keeps running, so its history stays true, but it shows as a grey "Paused" with your reason, never pages, and is left out of every count of problems. Pausing a check that already paged sends no "resolved". Resuming one that is still down pages again. Any signed-in user can pause, whatever the config's source, and the pause is in the history and in Argus's export. If Prometheus pages for you, `argus_check_paused` is 1 while a check is paused, and the rules on the Alerts page skip paused checks.

## Watching

**Checks, Watch more** puts every service worth watching on one page: each port your hosts' containers publish that no check covers (Argus tries each once to pick HTTP or TCP; exporters and brokers fold under "Usually not worth watching"), and each Proxmox server's web UI. Choose **Off**, **Watch** (a problem shows in Argus) or **Alert** (it also alerts you) for each, then **Add**. **A website or service** adds anything else, such as a public URL or a device; **Import** reads a pasted argus.yml, marks the checks you already have, and adds the ones you tick.

## Patching

**Hosts, Patching** sets it up for everything at once. Guests share one plan (a day, a starting time, all updates or security only, reboots, how recent the backup must be): **Turn on for N guests** applies it to every guest whose Proxmox backup is recent enough, and names the ones that have none. Each Proxmox server gets its own day, away from the guests' (the server Argus runs on goes last), and hosts with no backup Argus can see are listed with what to do. Any host can still be changed on its own page.

Each host's **Patching** tab has **Choose a window**: any weekday and a time on the host's own clock. The window lasts six hours, so with hosts taking turns the last of many guests still gets its turn; one whose turn never comes pages as skipped. In the window, the host's companion asks Argus, and Argus decides. It patches only a host with a safety net (below), one host at a time, and never a monitoring-only host. Updates run in a transient systemd unit, outside the companion's sandbox. Afterwards every container that ran before must run again (and be healthy, if it has a health check), then the host reboots if an update needs it and you allowed reboots. A failed run, a run with no result for two hours, or a window skipped for a stale backup pages you. **Patch now** runs once, outside the window, by the same rules.

**Already patched by something else** (Ansible, unattended-upgrades, your hypervisor's tools)? Choose **Something else patches it** on the host's card, and optionally the job that does it. Argus then never patches that host, so two tools never fight over it; the card shows how that job's last run went, the checklist counts the host as patched, and a failed or late run pages as any job does.

**Proxmox servers** are patched by a stricter rule. Their guests' backups are the safety net: a server is patched only when every guest on it has a backup within the days you set (if the update breaks the server, you reinstall Proxmox and restore them). A guest left out of backups on purpose can be marked **It needs no backup**. One server at a time, never while another server's newest run failed, always with all updates (`dist-upgrade`); Proxmox keeps the previous kernel for the boot menu, and a major version (9 to 10) is never taken on its own.

**No backup, no patching, unless you say otherwise.** A host backed up by something Argus cannot see (another person's hypervisor, say) can be marked **Backed up outside Argus**, with a note saying where: Argus takes your word and shows it as such ("a backup outside Argus (your word): …"). A host you could simply rebuild can be marked **It needs no backup** (on its Patching tab, or **Patch without a backup** in the Patching view): Argus patches it anyway and says so. Otherwise, since an update can break a machine, Argus patches a host only when it can be brought back:

- **a recent Proxmox backup**: on a Proxmox server, the companion reads each guest's newest vzdump backup from the server's backup storages (directory, NFS, CIFS or CephFS; Proxmox Backup Server is not read yet). The Proxmox server's card lists its guests and their backups, and a guest's own card picks its backup for you when the guest's name matches. Nothing to wire;
- **a recent backup** of another kind: a job or timer that reports to Argus (`POST /job/<name>` or `/heartbeat/<name>`) when it finishes. Name it on the host's card and say how recent it must be: 1 day for a nightly backup, 8 (the default) for a weekly one, 31 for a monthly one. An older backup skips that week's window, and Argus pages to say why;
- or **a ZFS root**, which Argus snapshots just before each run.

With either, Argus installs what you chose for the host (all updates, or security updates only) and reboots when an update needs it, if you allowed that.

**Restore tests** are how you know a backup works. Argus cannot open your backup to check it, but it can watch a restore test you run (a job that restores the backup somewhere and reports how it went). Name it on the card: Argus shows when it last passed and nudges you when it is older than 35 days. It is optional, and encouraged.

## Config

Everything Argus watches is one YAML file, `argus.yml`. Change single checks on the Checks page (**Add a check**, or **Change** in a check's panel), or the whole file on the Config page. Argus checks every change before saving; a mistake is shown and nothing changes. Comments and layout in the file are kept: an edit changes only its own lines.

Where the file lives (Config page):

- **In Argus** (a new install's default), with Export always there.
- **In a GitHub repo.** A change made in git shows within a minute; an edit made here becomes a commit, or a pull request to review. If the file changed in git since you opened it, the edit is refused and the newer version is shown. Saving needs the write token (GitHub page).
- **A mounted file** (`ARGUS_CONFIG`), read when it changes; the UI only shows it.

## Connect GitHub

On **Updates**, press **Connect GitHub**. Argus makes its own GitHub App on your account (or an organization) with GitHub's app-manifest flow: confirm on GitHub, then choose the repos it may see. Updates then shows each repo's pipeline: Dependabot's PRs, CI, the merge, the build and its Trivy scan. Majors are spotted from Dependabot's titles and wait for you.

- Read-only permissions, until you add the pipeline to a repo (below). The app's private key is kept encrypted in Argus and never shown.
- Argus polls GitHub every minute with conditional requests (a 304 costs no rate limit), so nothing needs to reach Argus from the internet.
- Cannot make apps? Paste a fine-grained read-only token on the GitHub page instead.
- **No CI yet?** Open a repo on Updates and press **Add the pipeline**. Argus reads the repo's Dockerfiles and shows what a pull request would add: `.github/workflows/ci.yml` (each image built, tested inside the image, started when it has a `HEALTHCHECK`, scanned with Trivy; Dependabot's patch and minor updates merged on green, majors labelled `waits-for-you`; each image pushed to GHCR as `sha-<7>` from the default branch) and, when the repo has none, `.github/dependabot.yml`. Edit the image names and test commands, then **Open the pull request**; you merge it on GitHub. The first time, Argus links you to the app's permissions on GitHub: set Contents, Pull requests and Workflows to Read and write, and accept the change. Each pull request then uses a key for that one repo, kept for nothing else. A repo with a `ci.yml` Argus did not write is left alone.
- Archived repos are skipped; hide others on the GitHub page. In `argus.yml`, `repos:` can link a repo to its check (`{name: shop, check: web}`).

## Add a host

On **Hosts**, press **Add a host** and run the command it shows on the host. It installs the companion (a single static binary, amd64 or arm64) as a systemd service. Within a minute the host shows its containers, disks, failed systemd units, pending updates and whether it needs a reboot.

- The companion only calls Argus; nothing on the host listens. Its first call pins Argus's TLS key, and every reply from Argus is signed with a key it pins at enrollment.
- The code in the command works once, for 15 minutes; a command made for several hosts (up to 50) works once per host, for an hour, and each host takes its own name. Removing a host revokes its token at once.
- A check with `companion: <host>` in `argus.yml` runs from inside that host, for services Argus cannot reach.
- A host that misses three reports, or a disk past 95%, pages.
- `argus-companion report` prints what it would send, without sending it.

## Deploys

**Set up deploys** (the checklist, or GitHub, Deploys) walks through it in four steps:

1. **Where your stacks live**: a private repo. **Create one on GitHub** opens GitHub's new-repo page with `homelab-stacks` filled in; an empty repo is fine, Argus writes its first commit.
2. **Let Argus write to it**: a fine-grained token for that repo only, with Contents read and write. Argus checks it can push before keeping it.
3. **Move your stacks in**: every Compose project running on your hosts outside Argus, ticked, grouped by host. **Move into git** has each host send its files, commits each project's compose file to `sites/<host>/<project>/` and adds it to `hosts/<host>`, and keeps its `.env` secrets in Argus. Containers keep running where they are. A project that cannot move says why (a secret written into the compose file, a folder outside the host's policy). Argus's own stack stays out: it updates from Settings.
4. **Keep them up to date**: **Turn on Dependabot** commits `.github/dependabot.yml`, so a newer image arrives as a pull request; merging it deploys it.

One repo says what each host runs: `hosts/<host>` lists stack folders (one per line), and each folder holds a `compose.yml` and any files next to it. Pick it on the GitHub page. Each host's companion deploys its stacks when they change: `docker compose pull`, then `up --wait` until healthy, and back to the previous files if it is not.

- **The host's policy decides.** `/etc/argus-companion/policy.json` on each host lists what it will never run: by default privileged containers, the host's network or PID namespace, the Docker socket, devices, risky capabilities, and binds from outside `/srv`, `/opt`, `/mnt`, `/media` or the stack's own folder. Argus cannot change it; a stack that breaks it is refused before anything is written, and pages.
- **Secrets** for a stack are set on the Hosts page, kept encrypted in Argus, and written to the stack's `.env` (root only) on the host that runs it, nowhere else.
- **Private images:** the companion pulls with a credential Argus sends with that deploy only; it is never stored on the host.
- Hosts enrolled **monitoring only** never deploy.
- Stacks live in `/var/lib/argus-companion/stacks/<name>`; data a stack keeps in its folder (`./data`) is never touched by a deploy.
- Without GitHub, `ARGUS_DEPLOY_DIR` points Argus at a folder with the same layout.
- **Tag bumps:** when a repo's main build is green, Argus rewrites `ghcr.io/<owner>/<repo>[-part]:sha-<commit>` in the deploy repo in one fast-forward commit, and the hosts deploy it. This needs a fine-grained token for the deploy repo alone (Contents: read and write); the GitHub App stays read-only. Updates then follows each change to "deployed" from the companions' own reports.

### Adopting stacks that already run

A host's **Deploys** tab lists the Compose projects running on it that git does not deploy yet. **Deploy from git** shows what would happen first: the compose files that go to the deploy repo, the secrets found in the project's `.env` (names only; their values are kept encrypted in Argus and never reach git), and the folder it stays in. Anything in the way is listed with its fix: no deploy repo or write token yet, a secret written into the compose file itself, or a folder outside what the host's policy allows. **Adopt** commits the files under `sites/<host>/<project>/` and adds it to `hosts/<host>`. The stack keeps running in its own folder, so data next to it (`./data`) never moves.

**Moving from another deploy tool.** When the deploy repo already lists a stack for a host and that stack is running there from a folder Argus does not manage (a git checkout another tool deploys from, say), Argus holds it back instead of starting a second copy, and the host's Deploys tab shows it as waiting to be linked. **Link** reads its folder and `.env` the same way, keeps the secrets in Argus, and from then on deploys git's version of it in that folder. Nothing is written to git, and the containers are not restarted when nothing changed. The companion refuses on its own to start a project that already runs from another folder.

Security: Argus only names the project. The companion finds its folder and compose files from Docker's own labels and reads only plain files inside that folder (no symlinks, at most 512 KB each); it later deploys in place only into folders it collected itself and its policy allows. Monitoring-only hosts never share files. What it collected is held in memory until you adopt it or 15 minutes pass.

## Back up Argus

Argus's data volume holds the key to every secret it keeps. **Settings, Account, Back up Argus** exports one file with the settings, secrets, hosts and Argus's certificate, encrypted under a passphrase you choose (scrypt and AES-256-GCM); it asks for the admin password again. **Restore from an export** on a new install carries on where the old one stopped: hosts keep reporting with their tokens, and replies are signed with the same key. Until an export exists, the home page reminds you.
