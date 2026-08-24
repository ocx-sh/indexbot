# Workflow invariants

`indexbot workflows-check` audits an index repository's `.github/workflows/`
tree for the structural properties that make the announce lane safe. Those
properties belong to the *repository's YAML*, not to this package's code, so
they cannot be asserted by a unit test — they are checked against the real
tree, in the repository's own CI.

```bash
indexbot workflows-check --dir .github/workflows --owner <org>
```

Exit `0` when every invariant holds, `1` when any does not; each finding is one
line on stderr naming the file and the rule. An empty directory is a failure,
not a pass — a check that silently audits nothing is what a wrong `--dir` looks
like.

`--owner` enables WF-07 only. Omit it and that one rule is skipped.

## Rules

| Rule | Invariant | Why |
|---|---|---|
| **WF-01** | Every workflow declares top-level `permissions: {}` | Without an explicit default-deny a workflow inherits the repository default, which may be write. A trailing comment is fine. |
| **WF-02** | Every `uses:` is pinned to a 40-hex commit SHA | A tag can be moved under you. `./` composite actions and `docker://` refs are exempt — neither is a marketplace ref. |
| **WF-03** | No workflow declares both `pull_request` and `pull_request_target` | Both fire on the same head commit, so one file carrying both must discriminate with a job-level `if: github.event_name == …` — and a job skipped by such an `if:` **still emits a check run**, conclusion `skipped`. GitHub counts `skipped` as satisfying a required status check and resolves duplicate context names to the most recent run, so the privileged half publishes a green-equivalent impostor of the unprivileged half's required context. |
| **WF-04** | No job `if:` compares `github.event_name` against a PR event, in a workflow with a PR trigger | The structural half of WF-03: with one trigger per file there is nothing for such a guard to decide, and reintroducing one is how the collision comes back. Comparisons against other events (`!= 'schedule'`) are ordinary and untouched. |
| **WF-05** | No step sets `ref:` in a `pull_request_target` workflow | A checkout with no `ref:` takes the base branch tip; an explicit `ref:` is the only way to reach PR head. The credentialed job must never execute untrusted content. |
| **WF-06** | A job holding `contents: write` under `pull_request_target` runs no `uses:` steps | Deferred writes to the base branch (arming auto-merge, dispatching a deploy) are safe only while the job holding that scope executes nothing. Other write scopes — `pull-requests`, `statuses`, `issues` — are legitimate on a gate job that labels, comments and publishes a check. |
| **WF-07** | Every job a `schedule:` can reach is upstream-guarded | A fork inherits every cron and runs it off its own stale YAML. Satisfied by `if: github.repository_owner == '<owner>'`, by `if: github.event_name != 'schedule'`, or by `needs:` on a job that is guarded — inheritance is transitive, since a skipped dependency skips its dependents. |

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
| `test_workflow_split.py::test_arm_auto_merge_job_never_checks_out`, `test_governance_gate_never_holds_contents_write` | WF-06 |
| the `github.repository_owner` guards asserted per workflow | WF-07 |
| `test_governance_contracts.py::test_g08_no_repository_dispatch_surface` (the `announce.yml`-absence half), `test_g17_no_announce_pat_surface` | **stays in the index repo** — a retired-surface absence test names a file that only that deployment ever had |
| `test_workflow_split.py`'s job-name, `gh`-invocation and incident assertions; `test_workflow_pathspec.py` | **stays in the index repo** — the arrangement and its witnesses live where the evidence is |

Everything in the "stays" rows still runs, in that repository, against its real
tree. What moved here is what holds for *any* index.

## Assumptions

Line scans keyed on GitHub Actions' conventional 2-space indentation, standard
library only — no YAML parser. The credentialed governance path must gain no
runtime dependency it does not already have, and a parser is a dependency.
Run `actionlint` in the same pipeline: it is what keeps the formatting
assumption true.
