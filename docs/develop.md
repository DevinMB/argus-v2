# Developing Argus

```bash
uv venv .venv && uv pip install -p .venv -r requirements-dev.txt
PYTHONPATH=src .venv/bin/pytest -q && .venv/bin/ruff check .
cd ui && npm ci && npm test
cd companion && go vet ./... && go test ./...
```

CI runs lint, tests, an image build (which runs the UI and companion tests), Trivy (fixable HIGH/CRITICAL fail), a container smoke test, and the Add a host command run for real on the runner.

## Words

Every string Argus shows follows [VOICE.md](VOICE.md): state first, then the action, with the exact name of the host, check or error. Plain words, short sentences, no em dashes, and never only an HTTP code.

## Releases

Versions are git tags, `v1.2.3` (design decision 21). **Actions, release, Run workflow** on `main` with `patch`, `minor` or `major` tags main's head with the next number and builds it; that build pushes `ghcr.io/devinmb/argus:1.2.3`, `:1.2` and `:1` and publishes a GitHub release whose notes list the pull requests merged since the last one. The first release is given as an exact version (`1.0.0`).

- **patch**: a fix, or a dependency update. The Dependabot gate starts one after each update it merges.
- **minor**: something new.
- **major**: something the owner has to act on, like a change to compose.yml. Installs on `:1` never pull a 2.x; Argus tells them it is out.

Every green build on `main` also pushes `sha-<7>` and `:edge`, and reports its version as `dev`.
