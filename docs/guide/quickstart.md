# Quickstart

This walks an index repository from empty to serving. The reference
deployment is [ocx-sh/index](https://github.com/ocx-sh/index) — read it
alongside this page; every file named here exists there.

## What an index repository holds

An OCX index is a static tree. The bot writes it; a static host serves it.
Nothing here is a server, and there is no database.

```text
p/<namespace>/<package>.json                    package root: governance + tags
p/<namespace>/<package>/o/sha256/<hex>.json     the OCI image index a tag resolved to
config.json                                     {"format_version": 1}
c/index.json                                    every package -> its root's digest
.github/index-policy.json                       THIS index's registry-host allowlist
.github/maintainers.yml                         reviewers the governance gate assigns
```

`p/**` is committed by contributors through pull requests. `config.json` and
`c/index.json` are rendered at deploy time and never committed.

## Install

```bash
uv tool install ocx-indexbot
```

In CI, pin it. The bot runs in privileged jobs, so its version belongs in a
committed lockfile where a bump is a reviewed pull request:

```toml
# bot-tools/pyproject.toml
[project]
name = "index-bot-tools"
version = "0"
requires-python = ">=3.12"
dependencies = ["ocx-indexbot==0.1.0"]
```

```bash
uv run --project bot-tools -- indexbot render --index-dir "" --out dist
```

## 1. State your registry policy

`.github/index-policy.json` is the allowlist of registry hosts a package root
may point at. It is a **committed file**, never a repository or Actions
variable: widening registry trust is a supply-chain decision, and "extend only
via reviewed pull request" is the control.

```json
{ "registry_hosts": ["ghcr.io"] }
```

There is no compiled-in default. An index that never states a policy fails
closed rather than silently inheriting someone else's. A host that no
registry adapter can fetch is refused at wiring time — allowlisting what
cannot be served produces roots that validate and then fail every download.

## 2. Seed a package

`seed-import` builds a first package root from a mirror's own metadata:

```bash
indexbot seed-import \
  --catalog-md   mirrors/kitware/cmake/CATALOG.md \
  --mirror-yml   mirrors/kitware/cmake/mirror.yml \
  --owner-github someone --owner-github-id 1234567 \
  --out p
```

Commit the result. From here on, tags arrive through `announce`.

## 3. Announce a tag

The publisher runs this — from a fork, with no write access to the index:

```bash
indexbot announce --package kitware/cmake --tags 3.31.0,3.30.5 --fork someone/index
```

It reads the tags from the physical registry, writes the image index it
resolved to as a content-addressed CAS object, updates the root, and opens a
pull request. `--out <dir>` writes the same files locally instead, for a dry
run.

The tag list is **owner-curated**: the bot records what the owner announces,
and CI verifies each claim against registry truth. It never enumerates a
registry and never invents a tag.

## 4. Gate the pull request

Two workflows, deliberately in two files (see
[Workflow invariants](../reference/workflow-invariants.md) WF-03):

- **unprivileged** (`pull_request`, no secrets, checks out PR head) —
  `indexbot validate <changed roots>` re-derives every claim the PR makes.
- **privileged** (`pull_request_target`, holds a token, never checks out PR
  head) — `indexbot classify-pr` routes the PR to the machine or human lane,
  and `indexbot governance-check` decides whether it may auto-merge.

Authorization comes from the **base branch's** committed `owners[].github_id`,
never from the PR's own content. A pull request editing its own `owners[]` is
a human-lane change and cannot self-authorize.

## 5. Render and deploy

```bash
indexbot render --index-dir "" --out dist
```

Emits `config.json`, the `/p/**` mirror, and `c/index.json` into `dist`.
Publish that directory to any static host.

Two operational rules for whatever serves it:

- **Never cache `*.json`.** The freshness contract is origin `ETag` +
  `If-None-Match`; a CDN cache in front of it breaks conditional GETs.
- Serve `c/index.json` — whole-catalog sync is a conditional GET plus a digest
  diff, not a crawl.

## 6. Keep it honest

```bash
indexbot reconcile
```

Nightly. It re-reads every committed root against the registry and **verifies
only** — it never writes a correction. A mismatch is an integrity anomaly, so
it files an issue and exits `65`, because a bot that silently "fixes" a
divergence destroys the evidence of how it happened.

Audit the workflows themselves in the same pipeline:

```bash
indexbot workflows-check --owner <your-org>
```
