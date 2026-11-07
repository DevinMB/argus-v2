# Security

Argus can change your hosts: it patches them and deploys to them. This page says what it holds, how that is protected, and where the limits are. If you find a hole, please report it privately through GitHub's **Report a vulnerability** on this repo, not in an issue.

## What Argus holds

- The admin password, hashed with Argon2.
- Secrets: the Telegram bot token, the GitHub App's private key or a pasted token, the deploy repo's write token, a registry read token, each stack's `.env` values, and Argus's own signing key. They are encrypted with AES-256-GCM under a key Argus makes on first start and keeps in its data volume, beside the database, so a copy of the database alone reveals nothing. The UI and API only ever say whether a secret is set.
- Each host's token, as a SHA-256 hash only.

## The web UI

- HTTPS from the first start. A fresh install can only be claimed with the setup code from its log.
- Viewing is open on your network by default; one switch on the Settings, Account page requires sign-in for everything. Changing anything always needs the password.
- Every change needs the session's CSRF token; the session cookie is HttpOnly and SameSite=Strict. Failed sign-ins are limited per address and overall.
- A Content-Security-Policy allows only Argus's own scripts and styles, and no framing.
- Exporting every secret (Back up Argus) asks for the password again.

## Companions

- **Nothing listens on a host.** The companion only calls Argus, over HTTPS, and refuses any server whose key does not match the pin it got at install. A proxy or a wrong DNS entry cannot impersonate Argus.
- **Every reply is signed** with Argus's ed25519 key, which the companion pinned at enrollment; the signature covers the host's id and a fresh nonce, so a reply cannot be replayed or sent to another host.
- **The host decides what it will run.** `/etc/argus-companion/policy.json` on each host refuses, whatever Argus says: privileged containers, the host's network, PID, IPC or user namespace, the Docker socket, devices, capabilities beyond a short list, `unconfined` security options, and bind mounts from outside `/srv`, `/opt`, `/mnt`, `/media` or the stack's own folder. Changing it takes someone on the host. A file that does not parse refuses everything.
- **Self-update is signed and pinned.** A companion replaces itself only with the build whose sha256 Argus put in its signed reply, downloaded over the pinned connection. A monitoring-only host never does, and `"manual_updates": true` in the host's policy turns it off.
- **Adding Argus to a Proxmox guest** (Add Argus on a guest row) has the server's companion run the install in that guest, as root, with `pct exec` or the QEMU guest agent. Argus sends only the VMID, the guest type, its own address, key pin and a one-time code; the companion checks each against a strict pattern and builds the command itself, so a reply cannot carry other commands. It installs curl when missing and nothing else. Root on a Proxmox server already reaches every guest, so this is a new path, not new power. A monitoring-only server never does it, and `"no_guest_installs": true` in the server's policy turns it off.
- **Updating Argus** (Settings, Update Argus) is done by the companion on Argus's own machine, never by Argus: Argus does not get the Docker socket. The signed reply names only a request id, an image and a Compose project; the companion refreshes that project only if it already runs that very image (so the request cannot restart anything else), pulls, recreates it, waits for Argus's health check and re-tags the previous image if the new Argus is not healthy. `"no_argus_updates": true` in that host's policy turns it off.
- **Monitoring only** is chosen at install, on the host; such a host never deploys, patches or shares files, whatever Argus says.
- The service runs with `ProtectSystem=strict` and writes only its own folder. Package updates run in a separate transient unit started for each patch run.
- Removing a host in Argus stops its token at once.

## Deploys and adopting stacks

- Stack secrets reach only the host whose list names the stack, inside the signed reply, and are written to a `.env` readable by root only.
- A registry credential comes with a deploy only when one is due, is used for that pull, and is deleted with the temporary Docker config.
- Adopting a running stack: Argus names the project only. The companion finds its folder and compose files from Docker's labels and reads only plain files inside that folder (no symlinks, 512 KB at most). It later deploys in place only into folders it collected itself and its policy allows. `.env` values never reach git; a compose file with a secret written into it is refused until the secret moves to `.env`.

## GitHub

- The GitHub App Argus makes for itself is read-only everywhere.
- Writing to git (tag bumps, adopting a stack, saving the config to a repo) uses a separate fine-grained token you give for those repos only. Argus checks it can push before keeping it, and commits only as fast-forwards.
- Argus polls; nothing needs to reach it from the internet.

## Limits

- **Argus's admin can change every host** it deploys to or patches, within each host's policy. Treat the admin password like a root password, and keep Argus off the internet.
- The data volume holds the key to every secret: back it up (or take an export) and keep the copy somewhere other than the machine Argus runs on.
- Viewing is open on your network unless you switch it off; the dashboard shows host names, addresses and container names.
- The companion runs as root, because Docker and package managers need it. Its policy limits what Argus can make it do; it cannot limit a person who already has root on that host.
