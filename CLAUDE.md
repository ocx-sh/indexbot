# CLAUDE.md

Guide for Claude Code in this repo.

## ⛔ MODEL POLICY — NON-NEGOTIABLE

Applies to EVERY subagent spawn (Agent tool, Workflow `agent()`, swarm skills).
Always set `model` explicitly — never rely on inherit.

| Task | Model |
|---|---|
| Implementation, research, review, docs, tests, exploration — **the default** | **Sonnet 5** (`sonnet`) |
| Genuinely hard problems where Sonnet demonstrably falls short | Opus (`opus`) — rare, justify in the spawn prompt |
| Synthesizing multiple agent results into architecture conclusions | Fable — main loop only, (near-)NEVER as a subagent |

## What this is

`ocx-indexbot` (PyPI) is the write path for an
[OCX](https://github.com/ocx-sh/ocx) package index: `announce | reconcile |
validate | render | seed-import | classify-pr | governance-check | workflows`.
It was extracted from [ocx-sh/index](https://github.com/ocx-sh/index), which is
now consumer #1 and the reference deployment.

Repo `ocx-sh/indexbot` · distribution `ocx-indexbot` · import `ocx_indexbot` ·
console script **`indexbot`**.

An index is one format with many copies. Every per-deployment input — the
registry-host allowlist, owners, the namespace prefix — is committed data in
the *index* repository, never a constant here.

## Highest-risk surface

The `announce` path processes untrusted fork-PR data under privileged CI. The
security bar for it is `.claude/rules/quality-indexbot-security.md` — read it
before touching `src/ocx_indexbot/**` or `.github/workflows/**`. Two rules from
it that shape everything else:

- **`fail_under = 100` branch coverage is a design constraint, not a target.**
  Never lowered, no inline `# pragma: no cover`.
- **Every governance contract G-01..G-20 carries a named test** in
  `tests/security/`. That enumerated suite is the audit artifact — deliberate
  DAMP duplication, not incidental drift.

## Toolchain (OCX dogfood)

`ocx.toml` pins `task`, `uv`, `git-cliff`, `actionlint`, `lychee`, `gitleaks`.
`direnv allow` (tracked `.envrc`) activates the project; CI does the same
through `ocx-sh/setup-ocx`, so both run identical `task <name>` commands.

| Task | Purpose |
|---|---|
| `task verify` | The gate — format, lint, types, bandit, tests + coverage, lockfile |
| `task test` | pytest only |
| `task wheel:check` | The built wheel actually ships `py.typed` |
| `task audit` / `task mutation` | pip-audit · mutmut baseline (advisory) |
| `task docs:build` | Strict MkDocs build |
| `task release:prepare` | git-cliff version bump + changelog + verify |

Never invoke a bare `ruff`/`pytest`/`pyright` — they resolve through `$PATH`
instead of the project pin.

## Wire contract = one-way door

Published URL shapes (`/config.json`, `/p/<ns>/<pkg>.json`,
`/p/<ns>/<pkg>/o/sha256/<hex>.json`, `/c/index.json`) and their JSON field
semantics are backward-compatible forever once clients bake the endpoint.
Additive changes only; `format_version` gates the rest. The schemas themselves
are OCX spec surface and live upstream — this package implements the write
side, and (ADR-4 BD-1) never imports a schema.

## Workflow

- **Branch + PR + merge** — never commit on `main`.
- Commits: [Conventional Commits](https://www.conventionalcommits.org/). No
  `Co-Authored-By` trailers.
- Docs: MkDocs Material → <https://ocx-sh.github.io/indexbot/>.
- Releases: `task release:prepare` → review → tag `vX.Y.Z` → PyPI Trusted
  Publishing (OIDC, no stored token).

## Rule catalog

`.claude/rules/` (installed via `grimoire.toml`'s `python-essentials` bundle,
plus the repo's own security rule).
