# Build your own pipeline

`indexbot ci` generates the pipelines for GitHub Actions and GitLab CI, and
most deployments should use it. This page is for everyone else: a forge it
does not generate for, a repository that already has a pipeline of its own, or
an operator who wants to know exactly what the generated files do before
trusting them.

The rule the whole surface is designed around: **one CI job runs one
`indexbot` command.** No job needs a `jq` filter, a `git` invocation, a
`gh`/`glab` call, or a shell loop. If yours does, you are reimplementing
something the bot already owns — and the bot's copy is the one with the
tests.

## What is *not* a lane: publishing

None of the five lanes below publishes. Announcing a tag — reading it from
the physical registry, storing the image index it resolved to as a
content-addressed CAS object, updating the package root and opening the fork
pull request — is [`ocx package announce`](https://github.com/ocx-sh/ocx)'s
job, and it runs in the **publisher's** pipeline, not the index's. indexbot
owns the index side: everything that happens to that pull request once it
exists.

0.5.0 removed this package's own `announce` subcommand for exactly that
reason (`adr_forge_neutral_owners.md` D3) — it was a second implementation of
the same byte-exact writer, in a second language, that nothing in production
called.

## The five lanes

An index repository has five independent lanes. They differ in what triggers
them and — the part that matters — in what they are allowed to touch.

| Lane | Trigger | Token | Runs |
|---|---|---|---|
| **Validate** | pull/merge request | **none** | `indexbot validate-pr` |
| **Govern** | PR event (GitHub) or a schedule (GitLab) | write | `indexbot governance-gate` / `governance-poll` |
| **Reconcile** | schedule | write (issues) | `indexbot reconcile` |
| **Render** | push to the default branch | deploy | `indexbot render` |
| **Housekeeping** | pipeline failure, schedule | write | `indexbot label-failed-run`, `indexbot stale` |

### Why validate and govern are separate jobs

The validate lane executes contributor-authored content: it checks out the
pull request's head. The govern lane holds a token that can write to the
index and merge. **Those two must never be the same job**, on any forge, for
any reason — a fork's pull request would then run its own code with the
index's credentials.

On GitHub the split is `pull_request` (no secrets, checks out the head) versus
`pull_request_target` (secrets, checks out the *base*, never the head). Both
triggers fire on the same commit, so they live in **separate workflow files**:
a job skipped by an `if:` still publishes a check run under its own name, and
GitHub counts a skipped required check as satisfied.

On GitLab there is no `pull_request_target`. A fork's merge-request pipeline
runs in the fork, under the fork's variables — the right boundary, and the
same one `pull_request` gives — and every feature that would put the parent's
variables on a fork MR does it by running the fork's own `.gitlab-ci.yml`.
So on GitLab the privileged actor is not MR-driven at all: it is a
**scheduled pipeline on the parent's default branch**, which is
`indexbot governance-poll`. Parent-authored config, parent-held token, no
merge-request content ever checked out.

## Lane by lane

### Validate — `indexbot validate-pr`

One job. Check out the pull request's head with full history
(`fetch-depth: 0` / `GIT_DEPTH: 0`), then run the command. It finds the
changed package roots itself, materializes each one's base-ref bytes, decides
the reserved-namespace carve-out from the request's provenance, and validates.

It needs **no token**. Give the job no secrets at all; that is the point of
the lane.

It reads `.github/index-policy.json` from the **base ref**, never from the
checkout. That file decides which paths this lane validates (`name_segments`),
which namespace segments it protects and which registry hosts it admits, and
the lane checks out the request's *head*: obeying the incoming branch's copy
would let a merge request pick the rules it is judged by. A merge request may
still propose a new policy — it is judged under the one in force, with a
notice saying so, and its proposal takes effect when it merges.

If the base ref carries no policy this version can read, nothing under `p/` is
judgeable, so the lane refuses any request that changes something there and
passes one that changes none — which is how the request that adopts or repairs
the policy stays mergeable.

```yaml
# GitHub Actions
- uses: actions/checkout@<sha>
  with:
    ref: ${{ github.event.pull_request.head.sha }}
    fetch-depth: 0
    persist-credentials: false
- run: indexbot validate-pr
```

```yaml
# GitLab CI
validate-pr:
  rules: [{ if: $CI_PIPELINE_SOURCE == "merge_request_event" }]
  variables: { GIT_DEPTH: "0" }
  script: [indexbot validate-pr]
```

### Govern — `indexbot governance-gate` / `governance-poll`

Same decision on both forges, reached two different ways.

- **GitHub**: `indexbot governance-gate --pr <number>` on
  `pull_request_target`. One PR, one run, one command.
- **GitLab**: `indexbot governance-poll` on a schedule. Every open merge
  request, one command. A failure on one merge request never ends the sweep;
  the run exits with the worst code it saw.

Both classify the change, label it, gate it, and arm the forge's own
auto-merge when the gate passes — bound to the revision that was gated, so a
push between the decision and the arm re-opens the question rather than
merging unreviewed content.

One command is the whole lane, and that is what you want. The generated GitHub
workflow splits it across two jobs anyway — `--no-arm` in one, `--arm-only` in
the other — for a reason worth copying if your forge lets you: the second job
runs even when the first *failed*, so a gate that errored still withdraws
whatever an earlier run armed. A single process that dies mid-gate leaves that
arming standing. If you cannot express "run this even if the previous step
errored", run the single command and accept that a crashed gate leaves a stale
arm until the next event.

```yaml
# GitHub Actions — taken from the generated governance.yml
on:
  pull_request_target:

jobs:
  governance-gate:
    outputs:
      disposition: ${{ steps.gate.outputs.disposition }}
    permissions:
      contents: read # base ref only, never PR-head content
      pull-requests: write
      statuses: write
      issues: write
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - id: gate
        env:
          GITHUB_TOKEN: ${{ github.token }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
        run: indexbot governance-gate --pr "$PR_NUMBER" --no-arm

  arm-auto-merge:
    needs: governance-gate
    # Fail-closed: a gate that ERRORED must still reach the withdraw, so this
    # cannot inherit the default success()-only condition a `needs:` implies.
    if: ${{ !cancelled() }}
    permissions:
      contents: write # arming/withdrawing is a deferred write to the base branch
      pull-requests: write
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - env:
          GITHUB_TOKEN: ${{ github.token }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
          DISPOSITION: ${{ needs.governance-gate.outputs.disposition }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
        run: >-
          indexbot governance-gate --pr "$PR_NUMBER" --arm-only
          --disposition "$DISPOSITION" --head-sha "$HEAD_SHA"
```

```yaml
# GitLab CI — taken from the generated .gitlab-ci/indexbot.yml
indexbot-governance-poll:
  variables:
    GIT_STRATEGY: none # API-only; never checks out merge-request content
  rules:
    - if: >-
        $CI_PIPELINE_SOURCE == "schedule" &&
        $INDEXBOT_LANE == "governance" &&
        $CI_PROJECT_NAMESPACE == "your-namespace"
  script:
    - indexbot governance-poll
```

The gate is fail-closed by construction: the commit status starts *absent*,
and an absent status already blocks. A merge request opened between two poll
ticks is unmergeable until the poller reaches it, never briefly mergeable.

### Reconcile — `indexbot reconcile`

A schedule, plus a manual trigger. It verifies committed index state against
registry truth and files an anomaly issue; it never auto-heals. Exit `65` is
an anomaly and means a human is needed — that is not the same as a failure.
Pass `--anomaly-ok` and the command makes that translation itself: it files the
issue, writes the same notice to the job summary, and exits `0`. Omit it if
your pipeline would rather branch on the raw code. Either way exit `75`
(transient, backoff exhausted) still fails, because the next scheduled run is
the retry.

```yaml
# GitHub Actions — taken from the generated reconcile.yml
on:
  schedule:
    - cron: "17 3 * * *" # ci.schedules.reconcile
  workflow_dispatch:

jobs:
  reconcile:
    permissions:
      contents: read # checkout only — verify-only reconcile never writes to p/
      issues: write # indexbot reconcile opens/updates the anomaly-tracking issue itself
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - env: { GITHUB_TOKEN: ${{ github.token }} }
        run: indexbot reconcile --anomaly-ok
```

```yaml
# GitLab CI — taken from the generated .gitlab-ci/indexbot.yml
indexbot-reconcile:
  # Unlike governance-poll/stale below, no GIT_STRATEGY: none — reconcile
  # reads the committed p/** tree from the checkout and verifies it against
  # the registry; the other scheduled lanes reach the forge API only.
  rules:
    - if: >-
        $CI_PIPELINE_SOURCE == "schedule" &&
        $INDEXBOT_LANE == "reconcile" &&
        $CI_PROJECT_NAMESPACE == "your-namespace"
  script:
    - indexbot reconcile --anomaly-ok
```

### Render — `indexbot render`

Runs on a push to the default branch, after any site build, and writes the
served tree into the directory the host deploys. It is the only lane that
needs deploy credentials — but that credential belongs to whatever ships the
rendered directory afterward (a `wrangler pages deploy`, a GitLab Pages job,
an `rsync` to your own host), never to `indexbot render` itself: the command
reads no environment variable at all (see
[`render`](../reference/cli.md#render)). Do not go looking for an
`indexbot render`-read token — none exists.

`indexbot ci` generates no Render job on either forge: the deploy step is a
hosting choice (Cloudflare Pages, GitLab Pages, an internal mirror), and a
generator that picked one would make every deployment ship the way the
generator's author happens to. This is the one lane with no rendered
reference implementation to fall back on, so wire it yourself. The GitHub
block below is distilled from `ocx-sh/index`'s own production
`render-deploy.yml` (real, Cloudflare Pages); the GitLab block is
illustrative — no reference GitLab render pipeline exists anywhere in this
project, so treat it as a starting shape, not a generated one.

```yaml
# GitHub Actions — distilled from ocx-sh/index's render-deploy.yml
on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  deploy:
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      # Any site build goes here, and it must run BEFORE render if it can
      # produce files render also owns (config.json, c/index.json) — render's
      # own pass is authoritative for those, so building second would either
      # clobber them or leave them missing.
      - run: indexbot render --index-dir "" --out dist
      - uses: cloudflare/wrangler-action@<sha>
        with:
          apiToken: ${{ secrets.CLOUDFLARE_API_TOKEN }}
          accountId: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
          command: pages deploy dist --project-name=your-project --branch=main
```

```yaml
# GitLab CI — illustrative only; no reference implementation exists for this forge
indexbot-render:
  stage: deploy
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  script:
    - indexbot render --index-dir "" --out public
    # Deploy `public/` with your own host's tooling here. GitLab Pages picks
    # up a job literally named `pages` with a `public/` artifact on its own;
    # any other host needs its own deploy step and its own credential.
  artifacts:
    paths: [public]
```

### Housekeeping

`indexbot label-failed-run` labels the pull request whose head failed a
pipeline. `indexbot stale` closes abandoned ones. Both reach the forge API
only — no third-party action required, and the same code on both forges.

The triggers are not the same, and cannot be:

- **`label-failed-run`** needs an event that fires *after* a pipeline failed,
  with the parent's credentials. GitHub has one — `workflow_run` on the
  validate workflow, `types: [completed]` — and it is what makes the fork case
  reachable there at all. GitLab has no counterpart: `when: on_failure` on the
  merge-request pipeline is the closest thing, and a fork MR's pipeline runs in
  the fork with no parent token. The generated GitLab job therefore requires
  the pipeline to be running in the *target* project, which is true for a fork
  MR only when the project enables "run pipelines in the parent project for
  merge requests from forks". **Leave that setting off.** It runs the *fork's*
  `.gitlab-ci.yml` in the parent's context, so every job in a fork-authored
  file holds the masked `$GITLAB_TOKEN` — `api` scope, able to label, comment
  and merge. Requiring a parent-project Developer to start the pipeline is a
  speed bump in front of a write credential, not a boundary. A `checks-failed`
  label on an abandoned fork MR is not worth that trade; let the lane stay
  inert on forks. Pass `--head-sha` explicitly on GitHub: the `$GITHUB_SHA`
  fallback names the default branch's head under `workflow_run`, not the head
  that failed.
- **`stale`** is a schedule on both, and needs the parent's token on both.

Both act on the same `checks-failed` label, so the second is inert wherever the
first cannot run.

On GitLab, `indexbot-label-failed-run` must run in stage `.post`. `when:
on_failure` fires only for a job in an *earlier* stage than the one that
failed — leaving it in the default `test` stage alongside `indexbot-validate`
would mean it could never fire at all, silently, with no error to notice.

```yaml
# GitHub Actions — two separate workflow files, taken from pr-checks-label.yml and stale.yml
on:
  workflow_run:
    workflows: ["validate"]
    types: [completed]

jobs:
  label-checks-failed:
    if: github.event.workflow_run.conclusion == 'failure'
    permissions:
      contents: read # checkout the default branch to run the pinned indexbot, never the completed run's commit
      pull-requests: write
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - env:
          GITHUB_TOKEN: ${{ github.token }}
          HEAD_SHA: ${{ github.event.workflow_run.head_sha }}
        run: indexbot label-failed-run --head-sha "$HEAD_SHA"
```

```yaml
# GitHub Actions — stale.yml, a separate workflow on its own schedule
on:
  schedule:
    - cron: "0 5 * * *" # ci.schedules.stale
  workflow_dispatch:

jobs:
  stale:
    permissions:
      contents: read
      pull-requests: write
      issues: write # stale/close comments post via the Issues API even for a PR
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - env: { GITHUB_TOKEN: ${{ github.token }} }
        run: indexbot stale
```

```yaml
# GitLab CI — taken from the generated .gitlab-ci/indexbot.yml
indexbot-label-failed-run:
  stage: .post # load-bearing — see above
  variables:
    GIT_STRATEGY: none
  rules:
    - if: >-
        $CI_PIPELINE_SOURCE == "merge_request_event" &&
        $CI_PROJECT_PATH == $CI_MERGE_REQUEST_PROJECT_PATH
      when: on_failure
  script:
    - indexbot label-failed-run

indexbot-stale:
  variables:
    GIT_STRATEGY: none
  rules:
    - if: >-
        $CI_PIPELINE_SOURCE == "schedule" &&
        $INDEXBOT_LANE == "housekeeping" &&
        $CI_PROJECT_NAMESPACE == "your-namespace"
  script:
    - indexbot stale
```

## Required repository settings

The bot posts a gate. Whether that gate *blocks* is the forge's decision, and
it comes from settings the bot cannot set for you.

=== "GitHub"

    - Branch protection on the default branch requiring the validate lane's
      check and `governance/review-required`.
    - Auto-merge enabled for the repository, or the arm does nothing.

=== "GitLab"

    - **Pipelines must succeed** — makes the external commit status blocking.
    - **All threads must be resolved** — this is what holds a *fork* merge
      request, whose pipeline runs in the fork. The bot's review-required
      notice is a resolvable discussion for exactly this reason, not a note.
    - **Pipeline schedules** on the default branch, one per lane, each carrying
      an `INDEXBOT_LANE` variable: `governance` (the poll lane — its interval
      is the gate's latency, not its strength), `reconcile`, and
      `housekeeping`.

    Approvals need no setting: an approval counts only when it was granted
    after the last push, which the bot establishes from server-side
    timestamps rather than from the Premium-only "remove approvals on push".

    One limit worth knowing, because no setting fixes it on Free: GitLab lets
    a merge request's own author resolve a thread on it, including the bot's.
    Doing so removes the visible block but merges nothing — auto-merge is
    armed only on a green gate, and a person still has to press merge. The
    poller re-opens the thread on the next tick. Required approvals and
    blocking status checks, which an author cannot lift, are Premium and
    Ultimate.

## Environment contract

Every subcommand builds its own ports at call time, so a job needs only what
its lane uses.

| Variable | Read by | Purpose |
|---|---|---|
| `GITHUB_TOKEN` / `GITLAB_TOKEN` | the lanes that write | forge API credential |
| `GITHUB_OUTPUT` | GitHub jobs | step outputs |
| `INDEXBOT_OUTPUT` | GitLab jobs | `dotenv` report path |
| `GITHUB_STEP_SUMMARY` | GitHub jobs | failure summaries |
| `GITHUB_API_URL` / `GITHUB_GRAPHQL_URL` | GitHub jobs | API roots — GitHub Enterprise Server sets both |
| *your* `credentials_env` names | `reconcile`, `seed-import` | `user:password` for a private registry. `indexbot ci` renders the GitHub passthrough; on GitLab set a masked, protected variable. Never reaches a fork-triggered job |

A command that finds no token runs read-only where that is meaningful
(`validate`, `render`) and fails with a clear message where it is not.

## Exit codes

Four values, and a pipeline should treat them differently.

| Code | Meaning | A pipeline should |
|---|---|---|
| `0` | applied, or nothing to do | pass |
| `1` | a semantic check rejected the input | fail |
| `65` | integrity anomaly, never auto-healed | surface to a human, not silently retry |
| `75` | transient, backoff exhausted | retry later |

`2` is argparse's own bad-invocation status.

## Pin the bot in the privileged job

Whatever forge you are on, the job that arms or performs a merge holds a token
that can move a branch and land a pull request. Whichever version of this bot
runs there must be decided by a reviewed commit, not fetched when the step
starts:

```yaml
# no
run: uvx ocx-indexbot governance-gate --pr "$PR_NUMBER" --arm-only
# no — `uv run` re-locks a stale lockfile, and a git source moves when it does
run: uv run --project bot-tools -- indexbot governance-gate --pr "$PR_NUMBER" --arm-only
# yes
run: uv run --project bot-tools --frozen -- indexbot governance-gate --pr "$PR_NUMBER" --arm-only
```

An exact specifier (`uvx --from 'ocx-indexbot==0.2.0' indexbot`) works too, as
does a bare `indexbot` your image or container entrypoint already resolved.
`indexbot ci` refuses to render a floating `ci.run`, and `indexbot
workflows-check` fails a hand-written pipeline on the same predicate
([WF-08](../reference/workflow-invariants.md)) — so the rule reaches you
whether or not you use the generator.

## Keeping a generated pipeline honest

If you do use `indexbot ci`, add its drift gate as a job:

```bash
indexbot ci --check
```

It fails when a generated file was hand-edited or when the policy that
generates it changed without a re-render. Without that job, "generated" is a
comment rather than a fact.
