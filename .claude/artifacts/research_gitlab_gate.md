# Research — how a GitLab index gates a merge (WP-0)

**Measured 2026-08-24** against a throwaway private project on gitlab.com
Free (`michael-herwig/indexbot-gate-probe`, id 85717462, deleted after).
The probe script was throwaway and is not shipped.

## Why this was measured and not designed

GitLab Free has no required merge-request approvals (Premium) and no blocking
external *status checks* (Ultimate). The docs describe external *commit
statuses* (`POST /projects/:id/statuses/:sha`, Free tier) as landing in an
`external` pipeline stage, but say nothing about whether they interact with
the `only_allow_merge_if_pipeline_succeeds` merge check. That interaction is
the entire GitLab governance lane, so it was measured rather than assumed.

## Q1 — Does an external commit status gate merge on Free? **Yes.**

Project setting `only_allow_merge_if_pipeline_succeeds: true`. No
`.gitlab-ci.yml` exists in the project, so the external status is the only
thing any pipeline state can come from. One MR, one head SHA, three POSTs:

| external status | `detailed_merge_status` | `PUT …/merge` |
|---|---|---|
| *(none posted)* | `ci_must_pass` | — |
| `pending` | `ci_still_running` | **refused**, HTTP 405 |
| `failed` | `ci_must_pass` | **refused**, HTTP 405 |
| `success` | `mergeable` | **accepted**, `state: merged` |

`GET …/commits/<sha>/statuses` afterwards returns exactly one row,
`governance/review-required -> success` — GitLab collapses repeated statuses
of the same name, so the three POSTs drove the three transitions above.

### Consequences for the design

1. **`ForgePort` needs no divergent gate method.** `set_commit_status` is the
   gate on GitHub *and* GitLab, with the same three states and the same
   meaning. The risk recorded in the plan ("if the answer is no, the poller
   can only fail an MR after the fact") is retired.
2. **The default is fail-closed, and more strongly than GitHub's.** With no
   status posted at all the MR sits at `ci_must_pass` and cannot be merged.
   On GitHub an unconfigured required context does not block; here the merge
   check blocks until something reports success. A governance poller that
   crashes leaves MRs unmergeable rather than unguarded.
3. **`merge_status` is the wrong field to read.** It stayed `can_be_merged`
   through every state, including the two that refused the merge — it reports
   only whether the branches merge cleanly. `detailed_merge_status` is the
   field that names the block, and it is what the GitLab adapter must consult.
4. The `pending` → `ci_still_running` mapping means a poller can pre-block a
   freshly opened MR by posting `pending` before it has decided anything,
   which is the same shape as `governance-check`'s existing `pending`
   disposition on GitHub. No new lane semantics.

## Q2 — Minimum pipeline-schedule interval

`POST /projects/:id/pipeline_schedules` accepted `*/5 * * * *`,
`*/15 * * * *` and `0 * * * *` on Free.

Caveat, not yet measured: API acceptance is not execution cadence. gitlab.com
applies plan limits (`ci_daily_pipeline_schedule_triggers`) and the schedule
worker has its own tick. **Real cadence is a WP-8 measurement** against the
live e2e index, not something this probe settles.

## Q3 — Fork MR pipelines and protected variables: deferred, not skipped

The claim (a fork MR pipeline cannot read the parent's protected+masked CI
variables, because fork MR pipelines run in the fork project with the fork's
variables) is explicit and unambiguous in GitLab's own documentation, unlike
Q1. It is also already a named scenario in WP-8's e2e, where it runs against
the real index rather than a throwaway. Measuring it twice buys nothing, so
it is proven there.

The security argument it supports is unchanged: on GitLab the privileged
governance actor is a **scheduled pipeline on the parent's default branch** —
parent-authored config, parent token, never reading fork-authored CI config —
because the only mechanism that would put parent variables on a fork MR event
executes the fork's `.gitlab-ci.yml`, which is the exact footgun
`pull_request_target` exists to avoid.

## Sources

- [Merge request approvals](https://docs.gitlab.com/user/project/merge_requests/approvals/) — approvals are optional on Free
- [External status checks](https://docs.gitlab.com/user/project/merge_requests/status_checks/) — Ultimate, non-blocking widget
- [External commit statuses](https://docs.gitlab.com/ci/ci_cd_for_external_repos/external_commit_statuses/) — Free, and what Q1 measured
- [Merge request pipelines](https://docs.gitlab.com/ci/pipelines/merge_request_pipelines/) — fork pipelines run in the fork


---

## Amendment, 2026-08-25 — the WP-0 measurement has a boundary

WP-0 measured a **same-project** merge request: a branch in the project, an
external commit status against its head. That result stands. The e2e
(`e2e-indexbot-index`, MR !2) measured the case WP-0 did not, and it is
different in kind.

### A fork merge request is not gated by a commit status

| | same-project MR | fork MR |
|---|---|---|
| `POST /projects/:parent/statuses/:sha`, no `ref` | accepted | **404** — the commit reaches the parent only via `refs/merge-requests/<iid>/head`, and the status API cannot place it |
| same POST with `ref=refs/merge-requests/<iid>/head` | accepted | accepted |
| `detailed_merge_status` with that status `pending` | `ci_still_running` | **`mergeable`** |

The merge request's `head_pipeline` is the **fork's** pipeline
(`project_id` = the fork). A status posted in the parent creates a pipeline the
merge request is not associated with, so "pipelines must succeed" is satisfied
by the fork's own run.

That is worse than "the gate does not apply": the fork's pipeline is authored
by the fork's own `.gitlab-ci.yml`. Treating it as evidence would put the
parent's merge gate under the fork's control — the exact property
`pull_request_target` exists to avoid, arriving by another door.

### What does gate a fork merge request

`only_allow_merge_if_all_discussions_are_resolved` (Free tier) plus one
bot-owned **discussion**:

| state | `detailed_merge_status` |
|---|---|
| bot thread unresolved | **`discussions_not_resolved`** |
| bot thread resolved | `mergeable` |

The thread lives in the parent project and is created by the parent's token.
A plain note is not resolvable and blocks nothing — it must be a discussion.

**Amendment, 2026-08-25: the thread is not fork-proof, and the earlier text
here said it was.** GitLab grants resolve rights on a resolvable thread to
the noteable's author as well as to the thread's author and anyone with
Developer or above. A fork merge request's author is the noteable's author,
so they can resolve the bot's thread and make their own merge request read
`mergeable`.

What that does and does not buy them:

- It does **not** merge anything. Auto-merge is armed only by this bot, only
  on a `success` disposition, and merging by hand still needs a person with
  merge rights on the parent.
- It **does** remove the visible block, so a maintainer who merges on the
  strength of "no threads open" is merging something ungated.

The bot re-opens the thread on every poll tick where the disposition is not
`success`, so the window is one poll interval. There is no Free-tier
alternative that a merge request's own author cannot lift — required
approvals and blocking status checks are Premium and Ultimate respectively,
and the Draft flag is the author's own title. The honest statement is: on
GitLab Free the gate is fail-closed against *machinery* and advisory against
a determined author, and the last line is the human who presses merge.

### Consequences

- `ForgePort.create_comment` on GitLab opens a discussion, not a note, and
  `resolve_review_thread` releases it. On GitHub the first is an issue comment
  and the second is a no-op; the difference stays inside the adapters.
- `set_commit_status` gains `pull_request`, which GitLab turns into the `ref`
  a fork merge request's head needs. It remains a *report* there, not a gate.
- A generated GitLab pipeline's header now requires **both** project settings
  and says which one is the gate.


## Amendment, 2026-08-25 — the commit-status state machine, measured

Posted against a real commit on `michael-herwig/e2e-indexbot-index`, one
context, in this order:

| POST | result |
|---|---|
| `success` (first ever for the context) | 201 |
| `pending` after `success` | **201** — a new status record, not a refusal |
| `pending` again | **400** `Cannot transition status via :enqueue from :pending` |
| `success` after `pending` | 201 |

So the rule is narrower than "a status cannot go backwards": only re-posting
the state a context already holds is refused. That is the case
`adapters/gitlab_api.py` treats as a no-op, and it is the common one — the
human lane's steady state is `pending`, re-asserted on every poll tick.

Correcting an inference that reached a security review: `success` → `pending`
does **not** fail, so a maintainer approving and then revoking does not wedge
the gate. `cli/governance_check.py` still writes the blocking thread before
the status and releases it after, because a status write is an API call that
can refuse for reasons unrelated to the decision, and on a fork merge request
the thread is the only thing holding the merge.
