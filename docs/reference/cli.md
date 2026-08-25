# CLI reference

One console script, `indexbot`, with 15 subcommands: `announce`, `reconcile`,
`validate`, `validate-pr`, `ci`, `render`, `seed-import`, `classify-pr`,
`governance-check`, `governance-gate`, `governance-poll`, `label-failed-run`,
`stale`, `workflows-check`, `schema`. `--version` prints the installed
distribution version.

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
indexbot announce --index-repo REPO --package <ns>/<pkg>
                  (--tags a,b | --tags-file FILE) (--out DIR | --fork REPO)
                  [--forge github|gitlab] [--yank TAG]... [--unyank TAG]... [--yank-reason TEXT]
```

| Flag | Meaning |
|---|---|
| `--package` | `<namespace>/<package>` to announce |
| `--tags` / `--tags-file` | The **entire** curated tag list, comma-separated or from a file. Exactly one. Not additive — see below |
| `--out` | Write the root and new CAS objects under this directory. Exactly one of this or `--fork` |
| `--fork` | The fork to commit to and open a pull request from |
| `--index-repo` | **Required.** The index repository to announce into — `<owner>/<repo>` on GitHub, a namespace path or numeric project id on GitLab |
| `--forge` | Which forge hosts `--index-repo` and `--fork`. Defaults to the CI runner's own signal (`$GITLAB_CI`), then to `github` |
| `--yank` / `--unyank` | Mark or clear a tag's yank marker (repeatable) |
| `--yank-reason` | Reason recorded for every `--yank` in this run |

`--fork` needs a write-scoped token — `GITHUB_TOKEN` or `GITLAB_TOKEN`,
whichever forge. `--out` needs none: it reads the target index's committed
policy anonymously and writes locally, which is what makes a dry run possible
from anywhere.

`--index-repo` has no default. It used to be `ocx-sh/index`, which meant a
publisher whose index is somewhere else announced into the public one by
forgetting a flag — the same argument that removed the `ocx.sh` prefix default
from [deployment policy](policy.md).

The bot never enumerates a registry. It records the tags it is given, and each
one is verified: the tag must resolve, and the bytes stored at
`o/sha256/<hex>.json` must hash to the digest in their own path.

!!! warning "`--tags` replaces the curated set — it does not add to it"

    Every run writes the root's `tags` map from the list this run was given.
    A tag the root already carries and this run does not name is **dropped**,
    which is how a tag removed upstream leaves the index at all.

    So the shape a pipeline reaches for first — one `announce` per tag push,
    naming only the tag that was just pushed — publishes that tag and deletes
    every other one the package had:

    ```bash
    # WRONG: the root now carries 3.31.1 and nothing else.
    indexbot announce --package kitware/cmake --tags "$CI_COMMIT_TAG" --fork …
    ```

    Pass every tag that should survive, every time. Where that list lives is
    the publisher's to decide — `--tags-file` exists so it can be a committed
    file rather than a shell variable:

    ```bash
    indexbot announce --package kitware/cmake --tags-file tags.txt --fork …
    ```

    Yanking is the other direction and is not this: `--yank` marks a tag
    without removing it, and the marker survives a re-announce that still
    names the tag (G-05).

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

Takes an explicit file set, which a caller has to compute. `validate-pr` below
computes it and is what a pipeline should run.

## `validate-pr`

The whole unprivileged pull-request gate as one command: resolve the changed
package roots, materialize their base-ref bytes, decide the reserved-namespace
carve-out from the pull request's provenance, then run everything `validate`
runs. One job, one command — writing your own CI is a matter of running it.

```bash
indexbot validate-pr [--base-sha SHA] [--offline] [--same-repo-pr | --fork-pr]
```

| Flag | Meaning |
|---|---|
| `--base-sha` | The pull request's base commit. Defaults to the environment, below |
| `--offline` | Skip the network checks (digest-in-scope, ownership probe) |
| `--same-repo-pr` | The head branch lives in the index repository itself — admits the operator's own reserved brand segments |
| `--fork-pr` | The pull request comes from a fork — reserved brand segments stay blocked |

The base commit is resolved in order: `--base-sha`, then `$INDEXBOT_BASE_SHA`,
then `$CI_MERGE_REQUEST_DIFF_BASE_SHA` (GitLab), then `origin/$GITHUB_BASE_REF`
(GitHub, which needs `fetch-depth: 0` — a shallow clone has no merge base to
find). None of them resolving is an error, never an empty diff: a gate that
passes with nothing validated is worse than one that fails.

The deployment policy this gate obeys comes from the **base ref**, read
through git, never from the checkout. This is the one job that checks out
pull-request head content, and `.github/index-policy.json` steers everything
here: `name_segments` builds the pathspec below, `reserved_namespaces` names
the brand segments a fork may not claim, `registry_hosts` is the allowlist. A
pull request declaring `"name_segments": 3` would otherwise leave its own
two-segment root matching nothing and take the "no package-root changes" exit,
green, having validated a claim on a reserved segment.

A pull request may still *propose* a new policy — that file is committed
rather than a settings-page value precisely so that widening it takes a
reviewed pull request. Its roots are simply judged under the policy currently
in force, with a notice saying so, and the proposal takes effect when it
merges.

When the base ref carries no policy this command can read — a deployment that
has not adopted one, or one migrating across a schema this version no longer
parses — there is nothing to judge roots against. It then refuses any pull
request that changes a path under `p/` (the widest pathspec, CAS objects
included: without `name_segments` there is no root glob to build, and an
unvalidatable object is no safer than an unvalidatable root) and exits `0` on
one that changes none. That keeps the pull request which *adopts or repairs*
the policy mergeable, instead of making the one file the gate depends on the
one file only a direct push to the default branch can change.

The diff is `<base>...HEAD` — **three dots**, against the merge base, so a
branch cut before another pull request merged does not re-validate stale copies
of roots it never touched — filtered by a `:(glob)` pathspec built from the
deployment's `name_segments`, which selects package roots and never the CAS
objects under them. Deletes are excluded; every other status, symlink swaps
included, is validated.

Each changed root's base-ref bytes are written to a temporary directory outside
the checkout, because the PR-head tree is what `validate` byte-compares against
its own canonical serialization. A root absent at the base ref is not written,
so it reads as a new claim.

`--allow-reserved-namespace` is **not** a flag here. Reserved brand segments
(`reserved_namespaces` in [deployment policy](policy.md)) are admitted only for
a pull request whose head branch is in the index repository itself, because a
fork that could claim them could publish under the index's own brand and be
believed by every client resolving through it. This job holds no token, so
provenance comes from the runner:

| Forge | Same-repo when |
|---|---|
| GitHub | `pull_request.head.repo.full_name` in `$GITHUB_EVENT_PATH` equals `$GITHUB_REPOSITORY` |
| GitLab | `$CI_MERGE_REQUEST_SOURCE_PROJECT_PATH` equals `$CI_MERGE_REQUEST_PROJECT_PATH` |

Anything else — a deleted fork's null `head.repo`, an unreadable payload, an
environment neither pattern matches — is read as a fork. `--same-repo-pr` /
`--fork-pr` override the sniff for a pipeline this bot did not generate.

Claiming is what the gate covers. A fork pull request that merely *updates* a
root already committed under a reserved segment — the `ocx package announce
--fork` re-announce lane — is admitted by the base-ref bytes, and only while
the change stays announce-shaped.

Needs `git` on `$PATH` and a checkout deep enough to reach the base commit.

## `classify-pr`

Route a pull request to the machine lane or the human lane.

```bash
indexbot classify-pr --pr-number N
```

Writes `classification` as a job output. Reads the PR through the API —
never a checkout.

## `governance-check`

The privileged gate: ownership, review requirements, auto-merge disposition.

```bash
indexbot governance-check --pr-number N
```

Writes `disposition` as a job output, publishes a commit status, and
assigns reviewers from `.github/maintainers.yml`. Authorization is the base
ref's committed `owners[].github_id` — a numeric id, because a login can be
renamed and recycled.

`governance.auto_merge` in [deployment policy](policy.md) moves the line
between the two lanes and nothing else:

| Value | Green when |
|---|---|
| `owners` (default) | the change is a tag refresh **and** the author owns every touched root (G-19) |
| `never` | never — every change waits for a person |
| `always` | the change is a tag refresh, whoever opened it |

`always` drops the ownership check, so it is only coherent where the forge
already decides who may open a pull request at all. It does not widen what
counts as machine-lane: a new package still needs a human under every setting.

A committed maintainer other than the author, approving at the pull
request's current head, turns the status `success` whatever the dial says.
That is the human lane's exit, and it exists because on GitLab the commit
status **is** the merge gate: a `pending` nobody can turn green is not a
stalled merge request but a permanently unmergeable one. On GitHub it changes
nothing about who may merge — `governance/review-required` is deliberately not
a required check there — it only makes the status say what already happened.

GitHub records the commit each review was left on, so a stale approval does
not count. GitLab's approvals carry no commit, and its documented remedy —
**Remove all approvals when commits are added** — is Premium: on Free the
setting cannot be turned on at all. So freshness is established from two
server-generated timestamps instead, the newest diff version's (a push
creates one) and the `approved` project event's. An approval older than the
last push does not count, on any tier, with no setting to forget.

This subcommand never arms auto-merge. `governance-gate` and
`governance-poll` do.

## `governance-gate`

The same decision as `governance-check`, for one pull request, in one
process — classify, label, gate, assign review, and arm or withdraw the
forge's own auto-merge.

```bash
indexbot governance-gate --pr N [--no-arm | --arm-only [--disposition STATE] [--head-sha SHA]]
```

Plain `--pr N` is the whole thing, and is what a hand-written pipeline should
run. The two flags exist for one arrangement: a pipeline that keeps the merge
scope out of the job that classifies. It is what a privileged single-PR job
runs on GitHub, and what `governance-poll` calls per merge request on GitLab:
one implementation, so the two lanes cannot drift on what "gated" means.

| Flag | Meaning |
|---|---|
| `--no-arm` | Gate and publish `disposition`; arm nothing. The job needs no write scope on the base branch |
| `--arm-only` | Arm or withdraw from an already-published disposition. Classifies nothing, writes no label and no commit status |
| `--disposition` | With `--arm-only`: the gate's answer. Anything but `success` — the empty string a failed gate publishes included — withdraws |
| `--head-sha` | With `--arm-only`: the revision the gate judged, which the arm is bound to |

The generated GitHub lane uses both, and the reason is fail-closed withdrawal
rather than permission scoping. The arm job runs on `if: ${{ !cancelled() }}`,
so a gate that *errors* still reaches the withdraw and cannot leave an
already-armed pull request armed on an evaluation that never finished. A
single process that dies mid-gate has no such second chance. `--arm-only` reads
no deployment policy at all, for the same reason: a policy fetch would be one
more way for the withdraw not to happen.

Arming is bound to a revision on both forges (`expectedHeadOid` / `sha`), and a
moved head is not an error — the decision was about a revision that is no longer
current, and the next event or poll tick gates the new one. When every required
check is *already* green, GitHub refuses to arm at all; the command performs the
equivalent squash itself, pinned to the same revision and with no privilege the
armed route would not have had.

## `governance-poll`

The whole governance lane for a forge with no privileged pull-request trigger
— which means GitLab.

```bash
indexbot governance-poll
```

Takes no arguments: it classifies, labels, gates and arms **every** open merge
request, then exits with the worst code any single one produced. One merge
request's failure never ends the sweep.

### What actually blocks a merge on GitLab

Measured, not assumed — and the two cases differ, which is the whole reason
this lane looks the way it does:

| | same-project MR | fork MR |
|---|---|---|
| external commit status under "pipelines must succeed" | **blocks** (`ci_must_pass` / `ci_still_running`) | does **not** block — the MR's head pipeline is the *fork's* |
| unresolved bot thread under "all threads must be resolved" | **blocks** | **blocks** (`discussions_not_resolved`) |

So the gate is the thread, and the commit status is the report a human reads.
A fork's pipeline is authored by the fork, so treating its result as evidence
would put the parent's merge gate under the fork's control; the thread lives
in the parent project and does not.

Both project settings are Free-tier. Enable both.

Run it from a scheduled pipeline on the default branch. That is the only place
on GitLab where parent-authored config runs with the parent's token — a fork's
merge-request pipeline runs in the fork, and every feature that would put the
parent's variables on a fork MR does it by running the fork's own
`.gitlab-ci.yml`, which is exactly what `pull_request_target` exists to avoid.

Polling costs latency, not safety. A commit status starts out absent, and an
absent status already blocks the merge, so a merge request opened between two
ticks is unmergeable until the poller reaches it — never briefly mergeable.

## `ci`

Render this index's pipeline files from its committed policy, or check them
for drift.

```bash
indexbot ci [--check]
```

The workflows an index runs are not that index's business — they are the bot's
governance model expressed as YAML: which trigger the privileged half may use,
what it may check out, which pathspec selects a package root, what happens to a
fork PR whose checks failed. Copying them between repositories is how a
deployment ends up running a two-year-old version of a security argument it
never read.

| Forge | Renders |
|---|---|
| `github` | `.github/workflows/{validate,governance,reconcile,pr-checks-label,stale}.yml` |
| `gitlab` | `.gitlab-ci/indexbot.yml` — **included**, never the root `.gitlab-ci.yml`, so the repository keeps somewhere of its own for everything else |

Inputs are the `ci` block of [deployment policy](policy.md), and nothing else.
Every generated job is one `indexbot` command — the pathspec that tells a
package root from a CAS object, the base commit to diff against, the pull
request's provenance, the exit-code translation, all of it is decided by the
command at run time from the same committed policy. Re-rendering is how a
deployment picks up a *changed generator*, never how it becomes correct.

`--check` is the gate. Run it as a CI job of its own; a hand-edit that survives
is a security argument replaced by an opinion. Two things are deliberately
**not** drift: a bumped action SHA (your dependency bot's job — and a render
carries it forward rather than reverting it) and a header version bump.

Not generated, on purpose: whatever publishes the rendered tree. That is a
hosting choice — Cloudflare Pages, GitLab Pages, an internal mirror — and a
generator that named one would make every index deploy the way the public one
happens to. Point `ci.deploy_workflow` at yours and the GitHub lane gains the
job that dispatches it the moment a machine-lane PR merges.

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

Reads no environment variable and holds no forge credential — it is a pure
local transformation, `p/**` in, `--out DIR` out. The "deploy credentials"
the [CI guide](../guide/ci.md#render-indexbot-render) mentions for this lane
belong to whatever ships that directory afterward (a `wrangler pages deploy`,
a GitLab Pages job, an rsync to your own host) and are never read by
`indexbot` itself — do not go looking for an `indexbot render`-read token,
because none exists.

## `reconcile`

Verify committed state against registry truth.

```bash
indexbot reconcile [--package <ns>/<pkg>] [--anomaly-ok]
```

**Verify-only.** It never writes a correction: a divergence is filed as an
issue and exits `65`. Backoff exhaustion exits `75` — a transient failure is
not an anomaly, and the nightly run is the retry. Needs `GITHUB_TOKEN` and
`GITHUB_REPOSITORY`, or `GITLAB_TOKEN` and `CI_PROJECT_ID`, whichever forge —
it opens and updates the tracking issue itself.

`--anomaly-ok` exits `0` once the anomaly is filed, for a scheduled job whose
red run would otherwise mean "the sweep broke" rather than "the index and a
registry disagree, and here is the issue". The tracking issue is the report;
the exit code was the shell's translation of it, and both forges' pipelines
did that translation by hand before this flag existed. `75` is unaffected —
a run that could not finish is still a failure.

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

## `label-failed-run`

Label the pull request whose head commit a failed pipeline ran on.

```bash
indexbot label-failed-run [--head-sha SHA]
```

`--head-sha` falls back to `$GITHUB_SHA` / `$CI_COMMIT_SHA`, so a pipeline
needs no interpolation to call it. The pull request is resolved from the
commit through the forge API, and a request whose head has since moved on is
not the one that failed — it is skipped rather than mislabelled.

This is the whole of a job that was a `gh api` call and two `jq` filters, and
it now exists on GitLab too.

## `stale`

Close abandoned pull requests, on either forge, with no third-party action.

```bash
indexbot stale [--dry-run]
```

A sweep: warn, then close, commenting exactly once per transition through the
same marker idempotency `create_comment` uses everywhere else. `--dry-run`
reports what would change and writes nothing.

## `workflows-check`

Audit an index repository's hand-written CI tree. See
[Workflow invariants](workflow-invariants.md) for the rules.

```bash
indexbot workflows-check [--forge github|gitlab] [--dir DIR] [--owner ORG]
```

| Flag | Meaning |
|---|---|
| `--forge` | Which forge's tree to audit. Defaults to `github` |
| `--dir` | On `github`: the workflow directory (default `.github/workflows`). On `gitlab`: the directory holding included files, alongside the root `.gitlab-ci.yml` which is always read regardless of this flag (default `.gitlab-ci`) |
| `--owner` | Enables WF-07, the cron upstream-guard check. `github` only — `gitlab` has no rule here that reads it |

## `schema`

Write the JSON Schema for `.github/index-policy.json` to stdout. Takes no
arguments and reads nothing — the schema is package data, so the copy printed
is the grammar of the exact bot version you are running.

```bash
indexbot schema > .github/index-policy.schema.json
```

Pin it that way if you want the check in CI to track your pinned bot version;
point `$schema` at the published URL if you only want editor autocomplete. See
[Deployment policy](policy.md).

## Environment

`classify-pr`, `governance-check`, `governance-gate`, `governance-poll`,
`label-failed-run`, `stale` and `reconcile` all resolve a repository through
the same helper (`_forge_api`), and so all seven talk to whichever forge they
are *running on*, decided by the runner's own variables — not by
`.github/index-policy.json`'s `ci.forge`, which says where the index is hosted
and is what `indexbot ci` generates workflows from. `$GITLAB_CI` selects the
GitLab column; anything else uses GitHub.

| Variable | Read by |
|---|---|
| `GITHUB_TOKEN` | `announce --fork`, and the seven above |
| `GITHUB_REPOSITORY` | the seven above, as `<owner>/<repo>` — plus `validate-pr`, which compares it against the pull request's head-repository provenance rather than resolving a project through it |
| `GITHUB_WORKSPACE` | every filesystem-reading subcommand, as the checkout root (defaults to the working directory) |
| `GITHUB_OUTPUT` | `classify-pr`, `governance-check`, `governance-gate` (unless `--arm-only`, which publishes nothing) |
| `GITHUB_STEP_SUMMARY` | any failing subcommand; absent is not an error |
| `GITHUB_ACTIONS` | `validate-pr`, to decide whether its notice/error is a workflow-command annotation or a plain log line |
| `GITHUB_EVENT_PATH` | `validate-pr`, for the pull request's head-repository provenance |
| `GITHUB_BASE_REF` | `validate-pr`, as `origin/<branch>` when no base sha is given |
| `INDEXBOT_BASE_SHA` | `validate-pr`, on any CI — the forge-independent base commit |

`render` and bare `validate` appear in neither table on purpose: `validate`
takes its files as explicit arguments, and `render` reads no environment
variable and holds no forge credential at all — see [`render`](#render).

On GitLab CI:

| Variable | Read by |
|---|---|
| `GITLAB_CI` | the forge selector — set by every GitLab job |
| `CI_PROJECT_ID` | the same seven subcommands as the GitHub column's `GITHUB_REPOSITORY` — `classify-pr`, `governance-check`, `governance-gate`, `governance-poll`, `label-failed-run`, `stale`, `reconcile` |
| `GITLAB_TOKEN` | the same seven, plus `announce --fork`. **Not** `$CI_JOB_TOKEN`, which cannot write labels, notes or merge requests — set a project or group access token as a masked CI variable |
| `CI_API_V4_URL` | the API root; defaults to `https://gitlab.com/api/v4`, and every self-hosted runner sets it |
| `INDEXBOT_OUTPUT` | `classify-pr`, `governance-check`, `governance-gate` (unless `--arm-only`) — the path the job also declares as its `artifacts:reports:dotenv`. Names are upper-cased (`CLASSIFICATION`, `DISPOSITION`) because a dotenv report becomes a CI variable verbatim |
| `CI_MERGE_REQUEST_DIFF_BASE_SHA` | `validate-pr`, as the base commit |
| `CI_MERGE_REQUEST_SOURCE_PROJECT_PATH` | `validate-pr`, compared against `CI_MERGE_REQUEST_PROJECT_PATH` for provenance. **Not** `CI_PROJECT_PATH`, which names the project the pipeline runs in — for a fork merge request that is the fork, so it equals the source project every time |
| `CI_MERGE_REQUEST_PROJECT_PATH` | `validate-pr`, the target project of the merge request |

GitLab has no job-summary surface, so a failure reason goes to stderr, which is
the job log.
