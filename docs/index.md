# indexbot

The write path for an [OCX](https://github.com/ocx-sh/ocx) package index.

An OCX index is a static sparse HTTP tree — package roots at
`/p/<namespace>/<package>.json` pointing at content-addressed OCI image
indices, no server and no database. `indexbot` is the process that maintains
one: it validates announced roots against registry truth, regenerates derived
fields, enforces the governance contracts that let untrusted fork pull requests
announce safely, and renders the wire tree a static host serves.

!!! warning "Scaffold"

    The toolchain, quality gate and release pipeline are wired; the subcommands
    land with the extraction move from
    [ocx-sh/index](https://github.com/ocx-sh/index).

## Install

```bash
uv tool install ocx-indexbot
```

Pin it in CI rather than installing it loose — the bot runs in privileged
workflow jobs, so its version belongs in a committed lockfile where a bump is a
reviewed pull request.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `announce` | Record an owner-curated tag, CI-verified against the physical registry |
| `reconcile` | Verify committed index state against registry truth; file anomalies, never auto-heal |
| `validate` | The unprivileged PR gate — semantic checks a JSON Schema cannot express |
| `render` | Emit the served wire tree (`config.json`, `/p/**`, `/c/index.json`) |
| `seed-import` | Bulk-import package roots from mirror metadata |
| `classify-pr` | Route a pull request to the machine lane or the human lane |
| `governance-check` | The privileged gate: ownership, review requirements, auto-merge arming |
| `workflows check` | Assert the workflow security invariants over an index repo's `.github/workflows/` |

## Exit codes

Four values, and only these — a caller scripts against them.

| Code | Meaning |
|---|---|
| `0` | No-op, or applied |
| `1` | A semantic check rejected the input |
| `65` | Integrity anomaly requiring a human — never auto-healed |
| `75` | Transient failure, backoff exhausted — the caller may retry |
