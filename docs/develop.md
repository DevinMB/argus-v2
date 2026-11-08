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
