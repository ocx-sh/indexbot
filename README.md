# indexbot

The write path for an [OCX](https://github.com/ocx-sh/ocx) package index.

An OCX index is a static sparse HTTP tree — package roots at
`/p/<namespace>/<package>.json` pointing at content-addressed OCI image
indices, no server and no database. `indexbot` is the process that maintains
one: it validates announced roots against registry truth, regenerates derived
fields, enforces the governance contracts that let untrusted fork PRs announce
safely, and renders the wire tree a static host serves.

The reference deployment is [ocx-sh/index](https://github.com/ocx-sh/index)
(<https://index.ocx.sh>). Nothing here is specific to it: an index is one
format with many copies, and every per-deployment input — the registry-host
allowlist, the owners, the namespace — is committed data in the index
repository, never a constant in this package.

> **Status: scaffold.** The toolchain, quality gate and release pipeline are
> wired; the subcommands land with the extraction move from `ocx-sh/index`.

## Install

```bash
uv tool install ocx-indexbot
```

Pin it in CI instead of installing it loose — the bot runs in privileged
workflow jobs, so its version belongs in a committed lockfile where a bump is
a reviewed pull request.

## Commands

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

## Development

The toolchain is provisioned by OCX itself. Install it once:

```bash
curl -sSL https://setup.ocx.sh | sh
```

`ocx.toml` pins `task`, `uv`, `git-cliff`, `actionlint`, `lychee` and
`gitleaks`. Either prefix commands with `ocx run --`, or activate the project
(`direnv allow`, using the tracked `.envrc`) and run them bare:

```bash
task verify   # format, lint, types, bandit, tests at 100% branch coverage, lockfile
task test     # pytest only
task format   # apply the formatter
```

CI reaches the same state through `ocx-sh/setup-ocx`, so its steps run the
identical `task <name>`.

## License

Apache-2.0. See [LICENSE](LICENSE).
