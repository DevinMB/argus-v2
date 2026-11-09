# ruff: noqa: E501 - the templates are written out as GitHub reads them
"""The app pipeline Argus offers each repo (design decision 20, roadmap R11).

One click on Updates opens a pull request that adds .github/workflows/ci.yml
(and .github/dependabot.yml when the repo has none). The workflow is written
out in full for that repo, so it runs nothing from anyone else's repo:

  pull requests   build each image, run its tests inside it, start it and wait
                  for its HEALTHCHECK, scan it with Trivy (fixable HIGH and
                  CRITICAL fail the build)
  Dependabot PRs  once every image passes: patch and minor updates merge,
                  majors are labelled and wait for the owner
  main            the same checks, then the image goes to GHCR as sha-<7>

Each image's build cache is scoped to it and to the ISO week, so an OS upgrade
step in a Dockerfile really runs at least weekly (found 2026-11-09: a shared,
never-expiring cache kept bouy-lab's apt-get upgrade layer stale, and Trivy
failed every Dependabot PR on fixed Debian packages).
"""

from __future__ import annotations

import base64
import json
import posixpath
import re
from dataclasses import dataclass, field

import yaml

VERSION = 1
MARK = "# Made by Argus (pipeline "  # the first line; Argus offers updates while it stays
LABEL = "waits-for-you"
SKIP_DIRS = {"node_modules", "vendor", "test", "tests", "examples", "example", ".devcontainer",
             "testdata", "fixtures", ".git"}

# Pinned by commit; Dependabot's github-actions updates keep them current in the repo.
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
BUILDX = "docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069 # v4.4.1"
BUILD = "docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc # v7.4.0"
TRIVY = "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0"
METADATA = "dependabot/fetch-metadata@25dd0e34f4fe68f24cc83900b1fe3fe149efef98 # v3.1.0"


@dataclass
class Image:
    job: str  # the workflow job id
    image: str  # ghcr.io/owner/name
    context: str = "."
    dockerfile: str = "Dockerfile"  # relative to the context
    test_command: str = ""
    smoke: bool = True  # start it and wait for its HEALTHCHECK
    smoke_env: str = ""
    build_args: str = ""
    branch: str = ""  # the branch whose pushes publish it; "" is the default branch

    def view(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Plan:
    """What the pull request would add, and why not when it cannot."""

    kind: str  # none, argus, shared, own: what ci.yml is today
    images: list[Image] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    blocked: str = ""  # why there is no pull request to open

    def view(self) -> dict:
        return {"kind": self.kind, "images": [i.view() for i in self.images],
                "files": self.files, "notes": self.notes, "blocked": self.blocked,
                "version": VERSION}


def kind_of(ci: str | None) -> str:
    """What a repo's ci.yml is: none, argus (ours), shared (calls a shared
    app-ci.yml, a shared reusable workflow) or own (anything else)."""
    if ci is None:
        return "none"
    if ci.startswith(MARK):
        return "argus"
    if re.search(r"uses:\s*\S+/\.github/workflows/app-ci\.yml@", ci):
        return "shared"
    return "own"


def version_of(ci: str | None) -> int | None:
    m = re.match(re.escape(MARK) + r"(\d+)\)", ci or "")
    return int(m.group(1)) if m else None


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return s or "app"


def dockerfiles(paths: list[str]) -> list[str]:
    """Dockerfiles worth building: not under tests, examples or vendored code."""
    out = []
    for p in paths:
        parts = p.split("/")
        if parts[-1] == "Dockerfile" and not SKIP_DIRS & set(parts[:-1]):
            out.append(p)
    return sorted(out, key=lambda p: (p.count("/"), p))


def detect(owner: str, repo: str, paths: list[str], texts: dict[str, str]) -> list[Image]:
    """One image per Dockerfile, built from its own folder: ghcr.io/owner/repo for
    the root one, ghcr.io/owner/repo-<folder> for the others."""
    images: list[Image] = []
    jobs: set[str] = set()
    for p in dockerfiles(paths):
        ctx = posixpath.dirname(p) or "."
        name = _slug(repo) if ctx == "." else f"{_slug(repo)}-{_slug(posixpath.basename(ctx))}"
        job = "app" if ctx == "." else _slug(posixpath.basename(ctx))
        while job in jobs:
            job += "-2"
        jobs.add(job)
        text = texts.get(p, "")
        images.append(Image(job=job, image=f"ghcr.io/{owner.lower()}/{name}", context=ctx,
                            smoke=bool(re.search(r"^\s*HEALTHCHECK\s+(?!NONE)", text, re.M | re.I))))
    return images


def carry_over(ci: str) -> list[Image]:
    """The images a shared-workflow caller builds, with its settings, so moving to
    Argus's pipeline keeps every image name the deploy repo already uses."""
    try:
        doc = yaml.safe_load(ci) or {}
    except yaml.YAMLError:
        return []
    out = []
    for job, spec in (doc.get("jobs") or {}).items():
        if not isinstance(spec, dict) or "app-ci.yml@" not in str(spec.get("uses", "")):
            continue
        w = spec.get("with") or {}
        ref = str(w.get("push-ref", ""))
        branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""
        out.append(Image(job=_slug(str(job)), image=str(w.get("image", "")),
                         context=str(w.get("context", ".")),
                         dockerfile=str(w.get("dockerfile", "Dockerfile")),
                         test_command=str(w.get("test-command", "")),
                         smoke=bool(w.get("smoke", True)),
                         smoke_env=str(w.get("smoke-env", "")),
                         build_args=str(w.get("build-args", "")),
                         branch="" if branch == "main" else branch))
    return [i for i in out if i.image]


def _q(s: str) -> str:
    """A YAML scalar that means exactly s (JSON strings are valid YAML)."""
    return json.dumps(s)


def _job(i: Image, default: str) -> str:
    branch = i.branch or default
    lines = [
        f"  {i.job}:",
        "    runs-on: ubuntu-24.04",
        "    env:",
        f"      IMAGE: {_q(i.image)}",
        "    steps:",
        f"      - uses: {CHECKOUT}",
        "",
        "      # Runners have about 14 GB free; toolchains this never uses take 20 GB more.",
        "      - name: Free disk space",
        "        run: sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc"
        " /usr/local/.ghcup /opt/hostedtoolcache/CodeQL",
        "",
        f"      - uses: {BUILDX}",
        "",
        "      # The cache is this image's own and starts fresh each ISO week, so an OS",
        "      # upgrade step in the Dockerfile runs at least weekly.",
        "      - id: week",
        "        run: echo \"week=$(date -u +%G-W%V)\" >> \"$GITHUB_OUTPUT\"",
        "",
        "      - name: Build",
        f"        uses: {BUILD}",
        "        with:",
        f"          context: {_q(i.context)}",
        f"          file: {_q(posixpath.join(i.context, i.dockerfile))}",
        "          pull: true",
        "          load: true",
        "          tags: app:ci",
        "          build-args: |",
        "            COMMIT=${{ github.sha }}",
    ]
    lines += [f"            {a}" for a in i.build_args.splitlines() if a.strip()]
    lines += [
        f"          cache-from: type=gha,scope={i.job}-${{{{ steps.week.outputs.week }}}}",
        f"          cache-to: type=gha,scope={i.job}-${{{{ steps.week.outputs.week }}}},mode=max",
    ]
    if i.test_command:
        lines += [
            "",
            "      - name: Tests (inside the image)",
            "        env:",
            f"          TEST_COMMAND: {_q(i.test_command)}",
            "        run: docker run --rm --network none --entrypoint sh app:ci -c \"$TEST_COMMAND\"",
        ]
    if i.smoke:
        lines += [
            "",
            "      - name: Smoke (healthcheck)",
            "        env:",
            f"          SMOKE_ENV: {_q(i.smoke_env)}",
            "        run: |",
            "          set -eu",
            "          if [ \"$(docker inspect -f '{{if .Config.Healthcheck}}yes{{end}}' app:ci)\""
            " != yes ]; then",
            "            echo \"::error::the image has no HEALTHCHECK; add one, or delete this step\"",
            "            exit 1",
            "          fi",
            "          printf '%s\\n' \"$SMOKE_ENV\" > smoke.env",
            "          docker run -d --name smoke --env-file smoke.env app:ci",
            "          for i in $(seq 1 60); do",
            "            s=$(docker inspect -f '{{.State.Health.Status}}' smoke)",
            "            [ \"$s\" = healthy ] && { echo \"healthy after ${i}x2 s\"; exit 0; }",
            "            [ \"$(docker inspect -f '{{.State.Running}}' smoke)\" = true ] || break",
            "            sleep 2",
            "          done",
            "          echo \"::error::the container did not become healthy (last: $s)\"",
            "          docker logs --tail 50 smoke",
            "          exit 1",
        ]
    lines += [
        "",
        "      - name: Trivy (fixable HIGH and CRITICAL fail the build)",
        f"        uses: {TRIVY}",
        "        with:",
        "          image-ref: app:ci",
        "          severity: HIGH,CRITICAL",
        "          ignore-unfixed: true",
        "          exit-code: \"1\"",
        "",
        "      - name: Push to GHCR",
        f"        if: github.ref == 'refs/heads/{branch}' && github.event_name != 'pull_request'",
        "        env:",
        "          GH_TOKEN: ${{ github.token }}",
        "        run: |",
        "          set -eu",
        "          tag=\"sha-${GITHUB_SHA::7}\"",
        "          echo \"$GH_TOKEN\" | docker login ghcr.io -u \"$GITHUB_ACTOR\" --password-stdin",
        f"          for t in \"$tag\" {_slug(branch)}; do docker tag app:ci \"$IMAGE:$t\";"
        " docker push -q \"$IMAGE:$t\"; done",
        "          echo \"### Pushed \\`$IMAGE:$tag\\`\" >> \"$GITHUB_STEP_SUMMARY\"",
    ]
    return "\n".join(lines)


def render_ci(images: list[Image], default: str) -> str:
    branches = sorted({default} | {i.branch for i in images if i.branch},
                      key=lambda b: (b != default, b))
    names = ", ".join(i.image.rsplit("/", 1)[-1] for i in images)
    head = f"""{MARK}{VERSION}). Argus opened this as a pull request; edit it as
# you like. Argus offers updates to it while this first line stays.
#
# Builds {names}.
#   pull requests   build each image, run its tests inside it, start it and wait
#                   for its HEALTHCHECK, scan it with Trivy (fixable HIGH and
#                   CRITICAL fail the build)
#   Dependabot PRs  once every image passes: patch and minor updates merge,
#                   majors get the {LABEL} label and wait for you
#   {default:<15} the same checks, then each image goes to GHCR as sha-<7 chars>
#
# Values reach shell steps through env, never by ${{{{ }}}} inside a script.

name: ci

on:
  pull_request:
  push:
    branches: [{", ".join(branches)}]
  workflow_dispatch:

permissions:
  contents: write       # the gate merges Dependabot PRs
  pull-requests: write  # ...and labels majors
  packages: write       # push to GHCR
  actions: write        # the gate starts the {default} build after merging

jobs:
"""
    jobs = "\n\n".join(_job(i, default) for i in images)
    needs = ", ".join(i.job for i in images)
    gate = f"""

  # Dependabot pull requests only, and only after every image passed.
  gate:
    needs: [{needs}]
    if: github.event_name == 'pull_request' && github.event.pull_request.user.login == 'dependabot[bot]'
    runs-on: ubuntu-24.04
    steps:
      - id: meta
        uses: {METADATA}

      # A merge made with GITHUB_TOKEN starts no push workflow, so the gate starts
      # the {default} build itself (dispatch is the documented exception).
      - name: Merge patch and minor updates, then build {default}
        if: steps.meta.outputs.update-type == 'version-update:semver-patch' || steps.meta.outputs.update-type == 'version-update:semver-minor'
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          PR: ${{{{ github.event.pull_request.number }}}}
          UPDATE: ${{{{ steps.meta.outputs.update-type }}}}
        run: |
          set -eu
          gh pr merge "$PR" --repo "$GITHUB_REPOSITORY" --merge --delete-branch
          gh workflow run ci.yml --repo "$GITHUB_REPOSITORY" --ref {_q(default)}
          echo "### Merged #$PR ($UPDATE) and started the {default} build" >> "$GITHUB_STEP_SUMMARY"

      - name: Majors wait for you
        if: steps.meta.outputs.update-type == 'version-update:semver-major'
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          PR: ${{{{ github.event.pull_request.number }}}}
        run: |
          gh label create {LABEL} --repo "$GITHUB_REPOSITORY" --color FBCA04 \\
            --description "A major update: merge it after a look" 2>/dev/null || true
          gh pr edit "$PR" --repo "$GITHUB_REPOSITORY" --add-label {LABEL}
"""
    return head + jobs + gate


def render_dependabot(images: list[Image], paths: list[str]) -> str:
    """Weekly updates for each image's base, the app's packages and the actions."""
    found: dict[str, set[str]] = {"docker": set(), "pip": set(), "npm": set(), "gomod": set()}
    for i in images:
        found["docker"].add("/" + posixpath.dirname(posixpath.join(i.context, i.dockerfile))
                            .removeprefix("./").strip("/"))
    manifests = {"requirements.txt": "pip", "pyproject.toml": "pip", "package.json": "npm",
                 "go.mod": "gomod"}
    for p in paths:
        parts = p.split("/")
        if parts[-1] in manifests and not SKIP_DIRS & set(parts[:-1]):
            found[manifests[parts[-1]]].add("/" + "/".join(parts[:-1]))
    lines = [f"{MARK}{VERSION}): weekly updates; the pipeline merges patch and minor.",
             "version: 2", "updates:"]
    for eco in ("docker", "pip", "npm", "gomod"):
        for d in sorted(found[eco]):
            lines += [f"  - package-ecosystem: {eco}", f"    directory: {_q(d.rstrip('/') or '/')}",
                      "    schedule:", "      interval: weekly"]
    lines += ["  - package-ecosystem: github-actions", "    directory: \"/\"",
              "    schedule:", "      interval: weekly"]
    return "\n".join(lines) + "\n"


def plan(owner: str, repo: str, default: str, paths: list[str], texts: dict[str, str],
         ci: str | None, has_dependabot: bool, edits: list[dict] | None = None) -> Plan:
    """What the pull request adds for this repo, as the owner would review it."""
    kind = kind_of(ci)
    p = Plan(kind=kind)
    if kind == "own":
        p.blocked = ("This repo has its own .github/workflows/ci.yml. Argus does not replace "
                     "a workflow it did not write: rename yours to keep both, or delete it.")
        return p
    images = carry_over(ci or "") if kind == "shared" else []
    if not images:
        images = detect(owner, repo, paths, texts)
    if not images:
        p.blocked = "No Dockerfile in this repo, so there is no image to build."
        return p
    for e in edits or []:
        for i in images:
            if i.job == e.get("job"):
                if e.get("image"):
                    i.image = e["image"]
                if e.get("test_command") is not None:
                    i.test_command = e["test_command"].strip()
    p.images = images
    for i in images:
        if not i.smoke:
            p.notes.append(f"{i.image} has no HEALTHCHECK, so it is not started in CI. "
                           "Add one to its Dockerfile to check it starts.")
    p.files[".github/workflows/ci.yml"] = render_ci(images, default)
    if not has_dependabot:
        p.files[".github/dependabot.yml"] = render_dependabot(images, paths)
    else:
        p.notes.append("Its .github/dependabot.yml stays as it is.")
    return p


PR_TITLE = "Add Argus's CI pipeline"


def pr_body(p: Plan) -> str:
    names = "\n".join(f"- `{i.image}` from `{posixpath.join(i.context, i.dockerfile)}`"
                      for i in p.images)
    notes = "\n".join(f"- {n}" for n in p.notes)
    return f"""Argus opened this from its Updates page.

**What it adds:** `.github/workflows/ci.yml`{" and `.github/dependabot.yml`" if ".github/dependabot.yml" in p.files else ""}. On every pull request each image is built, tested inside the image, started (when it has a HEALTHCHECK) and scanned with Trivy. Dependabot's patch and minor updates then merge on their own; majors get the `{LABEL}` label and wait for you. On the default branch each image goes to GHCR as `sha-<7 chars>`.

**Images:**
{names}
{f"{chr(10)}**Worth knowing:**{chr(10)}{notes}{chr(10)}" if notes else ""}
Merge it when it looks right. Nothing in it runs code from outside this repo, except the pinned GitHub Actions it names.
"""


# --- GitHub: reading a repo, opening the pull request ---

API = "https://api.github.com"
BRANCH = "argus/pipeline"


class GitHubError(Exception):
    """Safe to show: never carries a token."""


def _h(token: str, raw: bool = False) -> dict:
    return {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "argus",
            "Authorization": f"Bearer {token}"}


async def _call(client, method: str, path: str, token: str, ok=(200, 201), **kw):
    try:
        r = await client.request(method, f"{API}{path}", headers=_h(token, kw.pop("raw", False)),
                                 timeout=20, **kw)
    except Exception as e:  # noqa: BLE001 - shown as the kind of failure only
        raise GitHubError(f"could not reach GitHub ({type(e).__name__})") from None
    if r.status_code not in ok:
        try:
            why = str(r.json().get("message", ""))[:200]
        except ValueError:
            why = ""
        raise GitHubError(f"GitHub said HTTP {r.status_code} on {method} {path.split('?')[0]}"
                          f"{': ' + why if why else ''}")
    return r


async def read_repo(client, token: str, owner: str, repo: str, branch: str) -> dict:
    """What plan() needs: the file list, each Dockerfile, ci.yml, dependabot.yml."""
    base = f"/repos/{owner}/{repo}"
    tree = (await _call(client, "GET", f"{base}/git/trees/{branch}?recursive=1", token)).json()
    paths = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]
    texts = {}
    for p in dockerfiles(paths)[:20]:
        r = await _call(client, "GET", f"{base}/contents/{p}?ref={branch}", token, raw=True)
        texts[p] = r.text[:100_000]
    ci = None
    if ".github/workflows/ci.yml" in paths:
        ci = (await _call(client, "GET", f"{base}/contents/.github/workflows/ci.yml?ref={branch}",
                          token, raw=True)).text
    has_dependabot = any(p in paths for p in (".github/dependabot.yml", ".github/dependabot.yaml"))
    return {"paths": paths, "texts": texts, "ci": ci, "has_dependabot": has_dependabot}


async def open_pr(client, token: str, owner: str, repo: str, base_branch: str,
                  files: dict[str, str], body: str) -> str:
    """Put the files on argus/pipeline (made fresh from the default branch) and open,
    or reuse, the pull request. Returns its URL. Argus never merges it."""

    base = f"/repos/{owner}/{repo}"
    head = (await _call(client, "GET", f"{base}/git/ref/heads/{base_branch}", token)).json()
    sha = head["object"]["sha"]
    made = await _call(client, "POST", f"{base}/git/refs", token, ok=(201, 422),
                       json={"ref": f"refs/heads/{BRANCH}", "sha": sha})
    if made.status_code == 422:  # Argus's own branch from an earlier click: start it over
        await _call(client, "PATCH", f"{base}/git/refs/heads/{BRANCH}", token,
                    json={"sha": sha, "force": True})
    for path, text in files.items():
        old = await _call(client, "GET", f"{base}/contents/{path}?ref={BRANCH}", token,
                          ok=(200, 404))
        put = {"message": f"{PR_TITLE}: {path}", "branch": BRANCH,
               "content": base64.b64encode(text.encode()).decode()}
        if old.status_code == 200:
            put["sha"] = old.json()["sha"]
        await _call(client, "PUT", f"{base}/contents/{path}", token, json=put)
    prs = (await _call(client, "GET", f"{base}/pulls?state=open&head={owner}:{BRANCH}",
                       token)).json()
    if prs:
        return prs[0]["html_url"]
    pr = await _call(client, "POST", f"{base}/pulls", token,
                     json={"title": PR_TITLE, "head": BRANCH, "base": base_branch, "body": body})
    return pr.json()["html_url"]
