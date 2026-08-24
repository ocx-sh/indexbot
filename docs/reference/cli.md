# CLI reference

One console script, `indexbot`, with eight subcommands. `--version` prints the
installed distribution version.

Every subcommand builds its own ports at call time, so each one's environment
requirements are independent: a job running `validate` needs no token, and
importing the package never reads one.

## Exit codes

Four values, and only these. A caller scripts against them.

| Code | Name | Meaning |
|---|---|---|
| `0` | `OK` | No-op (nothing to do) or applied |
| `1` | `VALIDATION_FAILURE` | A semantic check rejected the input |
| `65` | `ANOMALY` | Integrity violation requiring a human — never auto-healed |
| `75` | `TRANSIENT` | Backoff exhausted; the caller may retry later |

`2` is argparse's own bad-invocation status and is left alone.

stdout carries a subcommand's result; diagnostics, progress and errors go to
stderr. A failure also lands a `## <subcommand> failed` block on
`$GITHUB_STEP_SUMMARY` when one is set — a publisher-visible failure must
never be a bare stderr line on a page nobody reads.

## `announce`

Record an owner-curated tag list, verified against the physical registry.

```bash
indexbot announce --package <ns>/<pkg> (--tags a,b | --tags-file FILE) (--out DIR | --fork OWNER/REPO)
                  [--index-repo OWNER/REPO] [--yank TAG]... [--unyank TAG]... [--yank-reason TEXT]
```

| Flag | Meaning |
|---|---|
| `--package` | `<namespace>/<package>` to announce |
| `--tags` / `--tags-file` | The curated tag list, comma-separated or from a file. Exactly one |
| `--out` | Write the root and new CAS objects under this directory. Exactly one of this or `--fork` |
| `--fork` | `<owner>/<repo>` fork to commit to and open a pull request from |
| `--index-repo` | The index repository to target |
| `--yank` / `--unyank` | Mark or clear a tag's yank marker (repeatable) |
| `--yank-reason` | Reason recorded for every `--yank` in this run |

`--fork` needs `GITHUB_TOKEN`. `--out` needs none — it reads the target
index's committed policy anonymously and writes locally, which is what makes a
dry run possible from anywhere.

The bot never enumerates a registry. It records the tags it is given, and each
one is verified: the tag must resolve, and the bytes stored at
`o/sha256/<hex>.json` must hash to the digest in their own path.

## `validate`

The unprivileged pull-request gate — the semantic checks a JSON Schema cannot
express.

```bash
indexbot validate [--offline] [--allow-reserved-namespace] [--base-dir DIR] <path>...
```

| Flag | Meaning |
|---|---|
| `paths` | The changed `p/<ns>/<pkg>.json` roots to validate |
| `--offline` | Skip the network checks (digest-in-scope, ownership probe) |
| `--allow-reserved-namespace` | Admit the operator's own brand segments only; control-path segments stay blocked |
| `--base-dir` | Directory holding each root's base-ref copy, so an announce-shaped update to a root already committed under a reserved segment is not read as a fresh claim |

Runs with no token and no write scope. It is the job that touches PR-head
content, which is exactly why it holds no credential.

## `classify-pr`

Route a pull request to the machine lane or the human lane.

```bash
indexbot classify-pr --pr-number N
```

Writes `classification` to `$GITHUB_OUTPUT`. Reads the PR through the API —
never a checkout. Needs `GITHUB_TOKEN` and `GITHUB_REPOSITORY`.

## `governance-check`

The privileged gate: ownership, review requirements, auto-merge disposition.

```bash
indexbot governance-check --pr-number N
```

Writes `disposition` to `$GITHUB_OUTPUT`, publishes a commit status, and
assigns reviewers from `.github/maintainers.yml`. Authorization is the base
ref's committed `owners[].github_id` — a numeric id, because a login can be
renamed and recycled. Needs `GITHUB_TOKEN` and `GITHUB_REPOSITORY`.

## `render`

Emit the served wire tree.

```bash
indexbot render --index-dir PREFIX --out DIR [--check]
```

| Flag | Meaning |
|---|---|
| `--index-dir` | Prefix *before* the literal `p/` component. `""` reads `p/**`; `demo` reads `demo/p/**` |
| `--out` | Output root |
| `--check` | Compare against what is already there instead of writing |

Produces `config.json`, the `/p/**` mirror and `c/index.json`. With no `p/`
tree yet it still emits `config.json` and an empty `c/index.json` — no
separate pre-seed code path.

## `reconcile`

Verify committed state against registry truth.

```bash
indexbot reconcile [--package <ns>/<pkg>]
```

**Verify-only.** It never writes a correction: a divergence is filed as an
issue and exits `65`. Backoff exhaustion exits `75` — a transient failure is
not an anomaly, and the nightly run is the retry. Needs `GITHUB_TOKEN` and
`GITHUB_REPOSITORY` (it opens and updates the tracking issue itself).

## `seed-import`

Bulk-import a package root from a mirror's own metadata.

```bash
indexbot seed-import --catalog-md FILE --mirror-yml FILE [--logo FILE]
                     [--namespace NS] [--package PKG] [--out DIR]
                     --owner-github LOGIN --owner-github-id ID
                     [--upstream-org ORG] [--upstream-repository-url URL]
                     [--upstream-disclaimer TEXT] [--repository OCI_URL]
                     [--allow-reserved-namespace]
```

`--namespace`/`--package` default to the shape of `--catalog-md`'s parent
directory. `--out` defaults to `p`.

## `workflows-check`

Audit an index repository's workflow tree. See
[Workflow invariants](workflow-invariants.md) for the rules.

```bash
indexbot workflows-check [--dir .github/workflows] [--owner ORG]
```

## Environment

| Variable | Read by |
|---|---|
| `GITHUB_TOKEN` | `announce --fork`, `classify-pr`, `governance-check`, `reconcile` |
| `GITHUB_REPOSITORY` | the same four, as `<owner>/<repo>` |
| `GITHUB_WORKSPACE` | every filesystem-reading subcommand, as the checkout root (defaults to the working directory) |
| `GITHUB_OUTPUT` | `classify-pr`, `governance-check` |
| `GITHUB_STEP_SUMMARY` | any failing subcommand; absent is not an error |
