# Workflow invariants

`indexbot workflows-check` audits an index repository's hand-written CI tree
for the structural properties that make the announce lane safe — GitHub's
`.github/workflows/`, or GitLab's root `.gitlab-ci.yml` plus whatever it
includes. Those properties belong to the *repository's YAML*, not to this
package's code, so they cannot be asserted by a unit test — they are checked
against the real tree, in the repository's own CI.

```bash
indexbot workflows-check --forge github --dir .github/workflows --owner <org>
indexbot workflows-check --forge gitlab --dir .gitlab-ci
```

Exit `0` when every invariant holds, `1` when any does not; each finding is one
line on stderr naming the file and the rule. An empty directory is a failure,
not a pass — a check that silently audits nothing is what a wrong `--dir` looks
like.

`--forge` defaults to `github`. `--owner` enables WF-07 only, and only on
`github` — GitLab has no rule here that reads it (see below).

## Rules (github)

| Rule | Invariant | Why |
|---|---|---|
| **WF-01** | Every workflow declares top-level `permissions: {}` | Without an explicit default-deny a workflow inherits the repository default, which may be write. A trailing comment is fine. |
| **WF-02** | Every `uses:` is pinned to a 40-hex commit SHA | A tag can be moved under you. `./` composite actions and `docker://` refs are exempt — neither is a marketplace ref. |
| **WF-03** | No workflow declares both `pull_request` and `pull_request_target` | Both fire on the same head commit, so one file carrying both must discriminate with a job-level `if: github.event_name == …` — and a job skipped by such an `if:` **still emits a check run**, conclusion `skipped`. GitHub counts `skipped` as satisfying a required status check and resolves duplicate context names to the most recent run, so the privileged half publishes a green-equivalent impostor of the unprivileged half's required context. |
| **WF-04** | No job `if:` compares `github.event_name` against a PR event, in a workflow with a PR trigger | The structural half of WF-03: with one trigger per file there is nothing for such a guard to decide, and reintroducing one is how the collision comes back. Comparisons against other events (`!= 'schedule'`) are ordinary and untouched. |
| **WF-05** | No step sets `ref:` in a **privileged** workflow — `pull_request_target`, `workflow_run` or `issue_comment` | A checkout with no `ref:` takes the base branch tip; an explicit `ref:` is the only way to reach contributor-controlled code. All three triggers hand a job the base repository's token on an event an outside contributor causes, so all three must never execute what that contributor wrote. `workflow_call` is deliberately excluded: a reusable workflow's privilege is its caller's, and the caller is where this audit can see it. |
| **WF-06** | A job holding `contents: write` under a privileged trigger sets `persist-credentials: false` on **every** checkout step, and hands no `${{ github.token }}` / `${{ secrets.* }}` to any `uses:` step | Two ways such a job leaks the token it was granted. A checkout taking the default `persist-credentials: true` writes it into `.git/config`, where every later step inherits it through plain `git` — dependency resolution and build backends included — with no `GITHUB_TOKEN` in sight to audit; the opt-out is read off each checkout's own `with:`, so a second checkout under a different `path:` cannot ride on the first one's hardening. Forwarding the credential into an action's inputs never touches `.git/config` at all and needs no checkout — it is the hazard the retired blanket `uses:` ban actually named. Other write scopes — `pull-requests`, `statuses`, `issues` — cannot move the base branch and are untouched. **This rule used to be stronger; see below.** |
| **WF-07** | Every job a `schedule:` can reach is upstream-guarded, **by the job's own `if:`** | A fork inherits every cron and runs it off its own stale YAML. Satisfied by `if: github.repository_owner == '<owner>'`, by `if: github.event_name != 'schedule'`, or by `needs:` on a job that is guarded — inheritance is transitive, since a skipped dependency skips its dependents. Matched against the job-level `if:` expression, not searched for anywhere in the job: a *step*-level `if:` skips one step and leaves the job running with its token, and a guard deleted but kept as a comment is the most plausible way this rule ever goes quiet. |
| **WF-08** | A job holding `contents: write` under `pull_request_target` runs no command that resolves the bot at job start | That job can move an unprotected base branch and squash-merge a pull request. `uvx ocx-indexbot` fetches whatever the index holds when the step starts — no version, no lockfile, no hash — so one malicious release executes with that token. Satisfied by a lockfile the command may not re-resolve (`--frozen`, `--locked`), by an exact version specifier, or by naming no resolver at all. **WF-02 does not cover this** — see below. |

## Rules (gitlab)

Only two, and deliberately not more. `indexbot ci --check` already gates
`.gitlab-ci/indexbot.yml` — the file `indexbot ci` generates — byte-for-byte
against the policy it renders from, so a property of *that* file (its
schedules are upstream-guarded, it sets `GIT_STRATEGY: none`/`GIT_DEPTH: 0`
where it must) is that drift gate's job, not this audit's; see
[What GitLab's drift gate already covers](#what-gitlabs-drift-gate-already-covers)
below. What has no gate at all is the root `.gitlab-ci.yml` an operator writes
by hand and whatever it includes — the GitLab analogue of a hand-written
GitHub workflow — and these two rules are the properties of *that* file with
no other gate.

| Rule | Invariant | Why |
|---|---|---|
| **GL-01** | Every `image:` is pinned to a digest — `<host>/<path>@sha256:<64-hex>` | A GitLab job's `image:` is the exact analogue of a GitHub `uses:` ref (WF-02): the code running in a credentialed job must not change without a diff. A mutable tag (`oven/bun:1-alpine`, `python:3.13`, or no tag at all) means it does. Checked both as a scalar (`image: foo@sha256:…`) and as a mapping (`image:` / `  name: foo@sha256:…`), and file-wide — a per-job override is checked the same as `default.image`. |
| **GL-03** | No job whose `rules:` reach `$CI_PIPELINE_SOURCE == "merge_request_event"` references a token-shaped variable in its `script:`, `before_script:` or `variables:` | A fork merge-request pipeline runs in the fork — the same trust boundary GitHub's plain `pull_request` gives — but only while the parent's credentials stay *protected* CI/CD variables. An operator who leaves `INDEXBOT_TOKEN` unprotected hands it to that fork pipeline, and no diff shows it: the exposure is a project setting, not a line of YAML. What IS visible in the YAML is the job shape that would matter if the variable were unprotected, so that is what is checked. `$CI_JOB_TOKEN` is exempt — GitLab's own per-job token, scoped to the project the pipeline runs in, which for a fork MR is the fork itself. |

**GL-02, GL-04 and GL-05 do not exist as `workflows-check` rules** — they are
properties of the *generated* `.gitlab-ci/indexbot.yml` (upstream-guarded
schedules, `GIT_STRATEGY: none`, `GIT_DEPTH: 0`), and `indexbot ci --check`
already gates that file byte-for-byte against the policy it renders from. A
static audit re-deriving them here would either duplicate that gate or drift
from it; the numbering skips them rather than filling the gap with a rule that
checks nothing a hand-edit could actually break.

GL-03 is read off each job's own block, not through `extends:`: a job whose
`merge_request_event` guard lives entirely on an extended template is not
caught. The generated templates never split `rules:` out that way; a
hand-written pipeline that does should keep the credential and the guard on
the job it actually appears in.

## Where these came from

The rules are the deployment-independent half of the security suite that used
to live in [ocx-sh/index](https://github.com/ocx-sh/index) and read that
repository's own workflow tree. This is the map:

| Was | Is now |
|---|---|
| `test_governance_contracts.py::test_g14_workflows_permissions_default_deny_and_sha_pinned` | WF-01, WF-02 |
| `test_workflow_split.py::test_no_workflow_declares_both_pr_triggers` | WF-03 |
| `test_workflow_split.py::test_no_job_if_discriminates_on_the_event_name_in_the_pr_workflows` | WF-04 |
| `test_workflow_split.py::test_pull_request_target_governance_job_never_checks_out_pr_head`, `test_governance_contracts.py::test_g16_privileged_unprivileged_split`, `test_threat_classes.py::test_threat_pr_target_no_head_checkout` | WF-05 |
| `test_workflow_split.py::test_arm_auto_merge_job_never_checks_out`, `test_governance_gate_never_holds_contents_write` | WF-06 (narrowed — see below) |
| the `github.repository_owner` guards asserted per workflow | WF-07 |
| `test_governance_contracts.py::test_g08_no_repository_dispatch_surface` (the `announce.yml`-absence half), `test_g17_no_announce_pat_surface` | **stays in the index repo** — a retired-surface absence test names a file that only that deployment ever had |
| `test_workflow_split.py`'s job-name, `gh`-invocation and incident assertions; `test_workflow_pathspec.py` | **stays in the index repo** — the arrangement and its witnesses live where the evidence is |

Everything in the "stays" rows still runs, in that repository, against its real
tree. What moved here is what holds for *any* index.

## What WF-06 used to say, and why it changed

It required a `contents: write` job under `pull_request_target` to run **no
`uses:` step of any kind** — no checkout, no composite action. The argument was
that a job which can move the base branch is safe only while it executes
nothing, and it held for as long as arming auto-merge was a `gh` one-liner.

It stopped being expressible when arming became `indexbot governance-gate
--arm-only`. Running the bot needs a checkout and a setup step, and both are
`uses:`. The rule had to either block the generator that this package ships or
narrow, and pretending a weaker rule carries the same weight would be worse
than either.

**What was given up.** The job that can move the base branch now executes
base-authored code and this package's hash-locked dependency tree. A compromise
of either reaches a token that can move an unprotected branch and merge a pull
request.

**What was not.** The code it runs is never pull-request-controlled — WF-05
forbids a `ref:` anywhere in a `pull_request_target` file, which is strictly
stronger than any per-job rule — and which *version* of it runs is decided by a
reviewed commit, which is WF-08. The residual exposure is supply chain, and it
is exposure a deployment already carries: the same pinned bot runs in the job
that renders and publishes the served index, which is what clients actually
consume. A token that can move a branch protected against it is the smaller of
those two.

**This page used to say WF-02 carried that second half. It did not.** WF-02
inspects `uses:` refs — it says nothing about how a `run:` command resolves a
*package*, and `uvx ocx-indexbot` is a `run:` command. So for as long as
`ci.run` defaulted to that string, the narrowing rested on a pin that was never
being checked: the actions were SHA-pinned and the bot was not. WF-08 is the
rule that makes the sentence true, and it arrived after the gap was found, not
with the narrowing.

**What replaced it** is the pair of ways such a job can leak the token it was
granted, which is what the blanket ban was a blunt instrument for:

1. The checkout half — `persist-credentials: false`, on every checkout step.
2. The hand-off half — no `${{ github.token }}` or `${{ secrets.* }}` passed
   into a `uses:` step. This is the half the retired rule's own rationale
   named (the setup action forwards the job's token to code the job does not
   control), and `persist-credentials: false` never reached it: no checkout is
   needed to hand an action a credential.

Forwarding is refused outright rather than allowlisted per action. The actions
such a job legitimately runs are the *deployment's* — a local
`./.github/actions/setup-bot`, here — so a deployment-independent allowlist
could only ever be a name, and a name is what a compromised release still has.
What is deployment-independent is that a job which can move the base branch has
no business handing that power to a step whose code it does not control. The
token belongs in the `run:` step that spends it, where a reviewer reading the
file sees it — which is exactly the shape `indexbot ci` renders.

A deployment whose `ci.run` needs no repository still renders the old shape —
a `contents: write` job with no checkout at all — and that passes WF-06
unchanged, because there is no `.git/config` to leak into and nothing being
handed a credential. It still answers to WF-08: how the bot got onto that
runner is the question WF-08 asks, and "no checkout" is not an answer to it.

## What WF-08 cannot see

A composite action the credentialed job `uses:`. Its steps live under
`.github/actions/**`, and `workflows-check` reads the top level of the workflow
directory and nothing below it, so a deployment that hides `uvx ocx-indexbot`
inside its `ci.setup` action passes this rule. That is a real gap and not a
rounding error: if you write your own setup action, the pin is yours to keep.

Folded scalars, too. WF-08 reads one line at a time — deliberately, so two
`run:` steps in one job cannot have the pinned one vouch for its neighbour —
which means a command split across lines by `run: >-` reads as unpinned. The
generated templates do not fold; a hand-written pipeline that does will see a
finding it can clear by putting the command on one line.

## What GitLab's drift gate already covers

`indexbot ci --check` gates `.gitlab-ci/indexbot.yml` — the file `indexbot ci`
generates — byte-for-byte against the policy it renders from. `workflows-check
--forge gitlab` never reads that file at all: a drift gate already proves it
is exactly what the policy renders, which is a stronger guarantee for a
generated file than a static audit could add. What the drift gate covers by
construction, so GL-01/GL-03 do not need to:

- **The `image:`.** `indexbot-governance-poll` holds `$GITLAB_TOKEN` (`api`
  scope) and executes whatever that image contains. The generated default is
  digest-pinned; a `ci.setup` of your own is on you to pin the same way, which
  is exactly what a hand-written `.gitlab-ci.yml` importing one is — GL-01's
  scope.
- **Every scheduled lane is upstream-only.** The generated rules carry
  `$CI_PROJECT_NAMESPACE == "<owner>"`, WF-07's counterpart (this package's
  hardening plan calls it GL-02). A hand-written schedule needs its own guard,
  and `workflows-check` does not check for one — add it yourself.
- **No merge-request lane holds a token.** `GIT_STRATEGY: none` on every
  privileged job (GL-04), and no privileged job triggered by
  `merge_request_event`. This is the split WF-05 enforces on GitHub; on GitLab
  it is a convention the generator follows, and GL-03 is the part of it that
  *is* checked for a hand-written file — the rest is not.
- **"Run pipelines in the parent project for merge requests from forks" stays
  off.** It runs fork-authored `.gitlab-ci.yml` in the parent with the parent's
  token — see the generated file's own comment on the `label-failed-run` lane.
  No rule here can see a project setting; this one stays a human's to check.

## Assumptions (github)

Line scans keyed on GitHub Actions' conventional 2-space indentation, standard
library only — no YAML parser. The credentialed governance path must gain no
runtime dependency it does not already have, and a parser is a dependency.
Run `actionlint` in the same pipeline: it is what keeps the formatting
assumption true.

Two places where a formatting assumption would have produced a *false clean*
are read from the file instead of assumed, because that failure direction is
the dangerous one:

- A job header may carry a trailing comment (`  arm-auto-merge:  # arms the
  merge`). Every job-scoped rule recognises it; a comment indented to the
  header column does not end the preceding job's block.
- A job's step list may sit at its key's own indentation (`    steps:` then
  `    - uses: …`), which GitHub runs and `actionlint` accepts. The step column
  is taken from the first item rather than assumed.

Known blind spots, all of them false *findings* rather than false cleans except
where noted:

| Written as | Read as |
|---|---|
| A folded job guard (`if: >-` continued on the next line) | not a guard → WF-07 fires. Write the guard on one line. |
| `persist-credentials: 'false'` (quoted) | not the opt-out → WF-06 fires. |
| A credential reaching an action indirectly — a job-level `env:` the action reads, or `${{ steps.*.outputs.token }}` from an App-token step | **not matched** (false clean). What WF-06 catches is the unremarkable `with: {token: …}` line; the indirect forms are visible in review. |
| A *negated* cron guard (`if: ${{ !(github.repository_owner == '…') }}`) | **not matched** (false clean). WF-07 reads the guard as a substring of the job's `if:`; telling a negation apart needs an expression parser. A deliberate act, not an accidental edit. |

## Assumptions (gitlab)

Same discipline: line scans keyed on GitLab's zero-indent top-level job keys,
standard library only. GL-01 and GL-03 are each read off one job's own block
(`job_block`), never through `extends:` — see GL-03's entry above for the
blind spot that leaves. A credential reaching a job indirectly (a `.gitlab-ci`
variable inherited from a group/instance CI/CD setting, never written in this
file at all) is invisible to a line scan the same way it is on GitHub.
