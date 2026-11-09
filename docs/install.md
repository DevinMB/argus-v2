# Installing Argus

## What you need

- A machine that is always on, with Docker: a NAS, a Pi 4 or 5, a small VM. Argus uses about 100 MB of memory.
- For hosts you want to know about (containers, disks, updates, patching, deploys): Linux on amd64 or arm64, with root. It runs as a systemd service, or an OpenRC one (Alpine); on Alpine, `apk add curl` first.
- Optional: a Telegram account for alerts, a GitHub account for updates and deploys.

## Start Argus

Use the `compose.yml` from the [README](../README.md#quick-start) (also in [examples/compose.yml](../examples/compose.yml)), then:

```bash
docker compose up -d
docker compose logs argus | grep "Setup code"
```

Open `https://<the machine>:8443`. The first visit asks for that setup code and a password of at least 10 characters. The code proves the person choosing the password can read Argus's log, so nobody else on your network can claim a fresh install.

### HTTPS

Argus serves HTTPS from the first start with a certificate it makes itself, and logs its fingerprint and key pin:

```
HTTPS on port 8080; certificate fingerprint (SHA-256) 3A:..; key pin I/30Fd...=
```

Browsers warn once about a certificate they do not know; compare the fingerprint if you like. Companions do not need a trusted certificate: the command that adds a host carries the key pin, and the companion refuses any other.

- **Your own certificate:** mount it and set `ARGUS_TLS_CERT=/certs/fullchain.pem` and `ARGUS_TLS_KEY=/certs/privkey.pem`.
- **Behind a reverse proxy that does HTTPS:** set `ARGUS_TLS=off` and let the proxy forward to port 8080. Companions then check the proxy's certificate the usual way.

## Settings from the environment

| Variable | Default | What it does |
| --- | --- | --- |
| `ARGUS_TLS` | `on` | `off` serves plain HTTP (behind a proxy) |
| `ARGUS_TLS_CERT`, `ARGUS_TLS_KEY` | | your certificate instead of Argus's own |
| `ARGUS_DB` | `/data/argus.db` | the database; everything Argus keeps is in its folder |
| `ARGUS_CONFIG` | `/config/argus.yml` | a mounted config file, if you prefer one (see [config.md](config.md)) |
| `ARGUS_DEPLOY_DIR` | | deploy from a folder instead of a GitHub repo |
| `ARGUS_PORT` | `8080` | the port inside the container |

Everything else is set in the UI.

## Add a host

On **Hosts**, press **Add a host**, copy the command and run it on the host. It downloads the companion from your Argus (checking Argus's key pin), enrolls with a code that works once for 15 minutes (or, made for several hosts, once per host for an hour), writes a default policy to `/etc/argus-companion/policy.json`, and starts the `argus-companion` service. The host appears within a minute.

Tick **Monitoring only** if this host should report but never deploy or patch.

A container you do not care about (a one-off job that exited, say) can be set aside with **Ignore** next to it on the host's Containers tab: it moves to the bottom and no longer counts as trouble. Stopped containers never page either way.

On a Proxmox server, Argus lists the guests in the list under the server. **Add Argus** on a guest (or **Add Argus to all** for the running ones) has the server install the companion in that guest for you: containers always, VMs when the QEMU guest agent runs in them (`apt install qemu-guest-agent`, and **Options, QEMU Guest Agent** on in Proxmox). Each guest joins under its Proxmox name. Home Assistant OS and other appliances without a shell cannot take a companion.

A host is named after its hostname. When that name is no good (two Proxmox servers both called `pve`, a VM that calls itself `localhost`), use **Rename** on its page. Its patch schedule and history come along, and nothing changes on the host. Deploys follow the new name: Argus sends it the stacks listed in `hosts/<new name>`.

### Uninstall a host

On the host's page, press **Uninstall** and enter the admin password twice. On its next report the companion hands back every stack Argus created as a plain Compose folder in `/opt/stacks/<name>`, with its `.env`, leaving the running containers alone. Then it removes itself: the service, `/usr/local/bin/argus-companion`, `/etc/argus-companion` and `/var/lib/argus-companion`. Argus forgets the host once the companion reports that it is done. Stacks Argus took over in place were never moved and stay where they are.

A host that is offline gets the command to run on it instead:

```bash
sudo /usr/local/bin/argus-companion uninstall
```

**Remove** only forgets the host (its token stops working at once) and is for a host that is gone for good.

Add the host again later and its handed-back stacks are taken over where they are. It joins as a new host, without its old patch plan.

## Remove Argus

**Settings, Leave Argus** walks through it:

1. Export a backup, in case you come back.
2. Uninstall every companion at once (the admin password twice), as above.
3. A checklist of what lives outside Argus, with a link for each: the GitHub App, the deploy repo's Dependabot file and write token, the Telegram bots. Argus deletes nothing outside itself.

Last, beside Argus's compose.yml:

```bash
docker compose down -v
```

`-v` removes Argus's data volume, which holds its settings and secrets.

## Upgrade

Argus has version numbers (`1.4.2`): a patch release fixes something or updates a dependency, a minor release adds something, and a major release needs something from you, said in its release notes. The image tag `:1` follows every 1.x release; `:1.4` and `:1.4.2` stay put.

**Settings, Account, Argus version** shows the release you run and what changed in the newer ones. When Argus's own machine runs a companion (add it as a host), **Update Argus** has that companion pull the new image and restart Argus; if the new Argus is not healthy within three minutes, the old one comes back. The header shows **Update available** when there is something new. Without a companion there, run this beside Argus's compose.yml:

```bash
docker compose pull && docker compose up -d
```

A new major version is never pulled for you: the version panel says when one is out, and you move to it by changing the tag in compose.yml (`:2`) after reading its notes.

Companions upgrade themselves: when Argus serves a different companion build, each host downloads it from Argus over its pinned connection, checks it against the sha256 in Argus's signed reply, installs it and restarts, within a minute or two. Not on a monitoring-only host, and not where the host's policy says `"manual_updates": true`; those, and companions from before self-update, are upgraded with **Reinstall companion** on the host's page: a Proxmox guest's server does it for you, and for any other host the page shows the command to paste (Add a host's, without a code, which keeps the enrollment). A companion that has not caught up with Argus for three hours is marked **companion out of date**, with the same button.

## Back up Argus

**Settings, Account, Back up Argus** exports one encrypted file with everything a new install needs to carry on: settings, secrets, hosts and Argus's certificate. Keep it off the machine Argus runs on. The home page reminds you until there is one. To restore, start a fresh Argus, sign in, and use **Restore from an export** on the same page.

## Forgot the password

```bash
docker compose exec argus python -m argus reset-login
```

Then open Argus and use the new setup code from its log.
