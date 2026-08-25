"""`indexbot governance-check` — sets the `governance/review-required`
required commit status, plus G-19/G-20 reviewer assignment (fork-PR announce
revamp, owner-confirmed decision set 2026-07-18).

Re-derives the PR's classification via `cli/classify_pr.classify_pull_request`
rather than reading a label back (no `ForgePort.get_labels`-shaped method
exists, and `.github/workflows/governance.yml` invokes this as its own
process, separate from `indexbot classify-pr` — single-source-of-truth via
the shared pure function, not a second hand-rolled diff walk).

Disposition:

- **Machine lane** (`refresh`): green (`success`) requires the PR author's
  `github_id` to appear in `owners[]` of *every* touched package root — read
  from the **base** ref only (`ForgePort.get_file_contents`, never the PR
  head; the same untrusted-head-content trust boundary
  `cli/classify_pr.py`'s module docstring already documents for
  `governance-gate`'s `pull_request_target` context) — this is G-19. A
  refresh-classified PR whose author does not own every touched package
  falls back to the human lane below (`pending` + reviewers + comment)
  rather than merging unreviewed.
- **Human lane** (`new-package`/`human-review-required`, or a refresh PR that
  failed G-19): `pending` + reviewers assigned from committed
  `.github/maintainers.yml` (read from the base ref) + one idempotent
  comment — G-20. Never `failure`: nothing has actually gone wrong, the PR
  just needs a human before it may merge.
- **A maintainer's approval releases it.** Any committed maintainer other than
  the author — matched by `github_id`, never by login — approving at the PR's
  current head turns the status `success`.
  This is the human lane's exit and it exists because on GitLab the commit
  status *is* the merge gate: without it, a human-lane merge request has no
  way to turn green and is not stalled but permanently unmergeable. On GitHub
  it changes nothing about who may merge — that context is deliberately not a
  required check there — it only makes the status say what already happened.

`.github/index-policy.json`'s `governance.auto_merge` moves the line between
those two lanes, and nothing else: `owners` is the description above,
`never` sends every PR to the human lane, `always` accepts the `refresh`
classification without G-19. See `_disposition`.

Reviewers are every `.github/maintainers.yml` entry's `github` login, minus
the PR author (self-review carve-out — GitHub's API itself rejects
assigning a PR's own author as one of their own reviewers). The login is the
right field there and only there: `ForgePort.request_reviewers` assigns by
name, while the approval that *releases* the lane is matched on `github_id`
(see `_approver`). The comment uses
a hidden HTML marker (`<!-- indexbot:governance -->`) so a later
`governance-check` run on the same PR updates the existing comment in place
rather than reposting on every re-run.

Writes the resulting commit-status state (`"success"` or `"pending"`) as the
`disposition` job output (`cli/_common.write_ci_output`) —
`.github/workflows/governance.yml`'s `governance-gate` job reads this back
(`steps.governance_check.outputs.disposition`) to decide whether to arm
auto-merge, rather than re-reading the label `indexbot classify-pr` applied
(G-19 requires the *ownership-checked* disposition, not just the raw
`refresh`/`new-package`/`human-review-required` classification).

This subcommand never arms auto-merge itself. On GitHub that is a separate
job holding `contents: write`, which is the whole point of BD-5's split. On
GitLab there is no MR-driven privileged job at all, so `cli/governance_poll.py`
does it — see that module.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Final, cast

from ocx_indexbot.cli.classify_pr import classify_pull_request
from ocx_indexbot.core.maintainers import parse_maintainers
from ocx_indexbot.core.policy import AutoMerge, IndexPolicy
from ocx_indexbot.core.validate_entry import parse_package_root
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode

from ._common import write_ci_output

if TYPE_CHECKING:
    import argparse

    from ocx_indexbot.model import CommitStatusState, Owner, PullRequestInfo
    from ocx_indexbot.ports import ForgePort

_STATUS_CONTEXT: Final[str] = "governance/review-required"
_MAINTAINERS_PATH: Final[str] = ".github/maintainers.yml"
_COMMENT_MARKER: Final[str] = "<!-- indexbot:governance -->"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `governance-check`'s CLI surface — `--pr-number`
    mirrors `cli/classify_pr.py`'s (trusted Actions-context value, no
    env-var-indirection discipline needed)."""
    parser.add_argument("--pr-number", type=int, required=True, help="pull request number to gate")


def _is_package_root_path(path: str, *, name_segments: int) -> bool:
    parts = path.split("/")
    return len(parts) == name_segments + 1 and parts[0] == "p" and parts[-1].endswith(".json")


def _author_owns_every_touched_package(
    info: PullRequestInfo, github: ForgePort, *, policy: IndexPolicy
) -> bool:
    """G-19: the PR author's `github_id` must appear in `owners[]` of every
    touched `p/<namespace>/<package>.json` root, read from the base ref
    (never the PR head).

    Filtering `changed_paths` down to roots is safe *here* only because this
    runs behind `classify_pull_request`, which already returned `"refresh"` —
    and that classification fails closed on any changed path outside those
    roots' refresh scope (ADR-6 FP-5). Do not reuse this filter anywhere that
    is not downstream of that gate.
    """
    root_paths = [
        path
        for path in info.changed_paths
        if _is_package_root_path(path, name_segments=policy.name_segments)
    ]
    for path in root_paths:
        base_raw = github.get_file_contents(path, info.base_sha)
        if base_raw is None:
            # No base-ref root at all — a genuinely new package, which
            # `classify_pull_request` already classifies "new-package" (the
            # human lane), never "refresh". Guards this helper's own
            # contract regardless of that upstream guarantee.
            return False
        root = parse_package_root(base_raw)
        if info.author_id not in {owner.github_id for owner in root.owners}:
            return False
    return True


def _disposition(
    change_class: str, *, author_is_owner: bool, approver: str | None, auto_merge: AutoMerge
) -> tuple[CommitStatusState, str]:
    """`(state, description)`. Never `failure`: a PR that needs a human has
    not gone wrong, so the gate stays `pending` until one arrives.

    `governance.auto_merge` is a dial over this one function, not a plugin
    seam — three values, one place, and every deployment still gets the same
    two states out:

    - **`owners`** (the default, and the public index's setting): green
      requires the `refresh` classification *and* G-19, the author owning
      every touched root.
    - **`never`**: nothing is ever green. An index that wants every change
      seen by a person, including a pure tag refresh by the package's own
      owner.
    A maintainer's approval outranks all three, and is the human lane's only
    exit. On GitHub `governance/review-required` is deliberately not a
    required check, so a person could always just merge; on a forge where the
    commit status **is** the gate, a human-lane PR with no way to turn green
    is not stalled — it is permanently unmergeable. `never` is included in
    what an approval outranks on purpose: it means "a person decides every
    change", not "nothing may ever merge".

    - **`always`**: the `refresh` classification alone is green. This drops
      G-19, so it is only coherent where the forge already decides who may
      open a PR at all — a private corporate index whose whole namespace is
      one team. It does **not** widen what counts as machine-lane:
      `new-package` and `human-review-required` still need a human, because
      those classifications exist to catch changes no ownership check would
      have caught either.
    """
    if approver is not None:
        return "success", f"{change_class}: approved by {approver}"
    if auto_merge == "never":
        return "pending", f"{change_class}: policy sets governance.auto_merge = never"
    if change_class != "refresh":
        return "pending", f"{change_class}: awaiting human review"
    if auto_merge == "always":
        return "success", "refresh: policy sets governance.auto_merge = always"
    if author_is_owner:
        return "success", "refresh: PR author owns every touched package, no review required"
    return "pending", "refresh: PR author does not own every touched package (G-19)"


def _committed_maintainers(github: ForgePort, base_sha: str) -> tuple[Owner, ...]:
    """`.github/maintainers.yml` at `base_sha`, or `()` on either a missing
    file or a malformed one — a corrupt committed file must never crash the
    gate itself, it just means G-20 can't name anyone to assign."""
    raw = github.get_file_contents(_MAINTAINERS_PATH, base_sha)
    if raw is None:
        return ()
    try:
        return parse_maintainers(raw)
    except ValidationError as exc:
        print(f"governance-check: malformed maintainers.yml ignored: {exc}", file=sys.stderr)
        return ()


def _eligible_maintainers(github: ForgePort, info: PullRequestInfo) -> tuple[Owner, ...]:
    """Every committed maintainer except the PR's own author, matched on
    `github_id`.

    The self-review carve-out is not politeness: GitHub's API rejects
    assigning a PR's author as their own reviewer, and an approval by the
    author would make the human lane a formality. It excludes on the numeric
    id rather than the login for the same reason `_approver` matches on one —
    a maintainer who has since renamed still authors PRs under the same id.
    """
    maintainers = _committed_maintainers(github, info.base_sha)
    return tuple(maintainer for maintainer in maintainers if maintainer.github_id != info.author_id)


def _reviewer_logins(github: ForgePort, info: PullRequestInfo) -> list[str]:
    """The eligible maintainers' **logins** — the one field that legitimately
    travels by name, because `ForgePort.request_reviewers` assigns reviewers
    by login (GitHub's API takes names, and the GitLab adapter resolves them
    back to ids at its own boundary). Asking a person to look is not
    authorization; `_approver` is, and it uses ids.
    """
    return [maintainer.github for maintainer in _eligible_maintainers(github, info)]


def _approver(github: ForgePort, info: PullRequestInfo) -> str | None:
    """The first committed maintainer, other than the author, who has approved
    this PR at its current head — or `None`.

    Matched by numeric `github_id`, never by login. This is the human lane's
    only exit and it outranks every disposition including
    `governance.auto_merge = never`, so a login match would let whoever
    acquires a renamed-and-released maintainer name release the gate. The
    login this returns comes back out of the committed `maintainers.yml` entry
    that matched, not out of the forge's approval payload — it is a label for
    the status description, never the thing that was compared.
    """
    eligible = {
        maintainer.github_id: maintainer.github
        for maintainer in _eligible_maintainers(github, info)
    }
    if not eligible:
        return None
    approvals = github.list_approvals(info.number, head_sha=info.head_sha)
    return next((eligible[user_id] for user_id in approvals if user_id in eligible), None)


def _assign_reviewers_and_comment(github: ForgePort, info: PullRequestInfo, *, reason: str) -> None:
    """G-20: reviewers from committed `.github/maintainers.yml` (base ref),
    minus the PR author (self-review carve-out), plus one idempotent
    comment explaining why review is needed."""
    logins = _reviewer_logins(github, info)
    if logins:
        github.request_reviewers(info.number, logins)
    github.create_comment(
        info.number,
        f"{_COMMENT_MARKER}\nThis PR requires human review: {reason}.",
        marker=_COMMENT_MARKER,
    )


def gate_pull_request(
    info: PullRequestInfo, change_class: str, github: ForgePort, *, policy: IndexPolicy
) -> CommitStatusState:
    """Decide, publish the commit status, and assign review if one is needed.

    Split out of `run` so `cli/governance_poll.py` gates a merge request with
    exactly this code and not a second implementation of it — the poll lane
    exists because GitLab has no privileged MR trigger, which is a difference
    in *when* the gate runs, never in what it decides.
    """
    author_is_owner = (
        policy.auto_merge == "owners"
        and change_class == "refresh"
        and _author_owns_every_touched_package(info, github, policy=policy)
    )
    state, description = _disposition(
        change_class,
        author_is_owner=author_is_owner,
        approver=_approver(github, info),
        auto_merge=policy.auto_merge,
    )
    # Order is a safety property, not style. The blocking artifact goes up
    # BEFORE the status, and comes down only AFTER it.
    #
    # A GitLab commit status is a state machine, and this gate re-runs against
    # the same head on every poll tick, so a status write is one API call that
    # can refuse for reasons that have nothing to do with the decision. If
    # that call is made first and raises, the run ends before the
    # review-required thread is re-opened — and on a fork merge request that
    # thread is the only thing holding the merge. Writing the block first
    # means the worst case is a merge request that blocks with a stale status
    # beside it, which is the direction to fail in.
    #
    # Measured on gitlab.com, 2026-08-25, since the exact state machine
    # decides how often this matters: `success` -> `pending` on the same
    # context is ACCEPTED (a new status record, 201), as is `pending` ->
    # `success`. Only re-posting the state already held is refused (400,
    # "Cannot transition status via :enqueue from :pending"), which
    # `adapters/gitlab_api.py` treats as the no-op it is. So the abort this
    # ordering protects against is the unexpected refusal, not a routine one.
    if state != "success":
        _assign_reviewers_and_comment(github, info, reason=description)
    github.set_commit_status(
        info.head_sha,
        context=_STATUS_CONTEXT,
        state=state,
        description=description,
        pull_request=info.number,
    )
    if state == "success":
        # Release whatever an earlier run left blocking. On GitHub this is a
        # no-op; on GitLab it is the merge gate itself, so it is released only
        # once the green status it reports is actually recorded.
        github.resolve_review_thread(info.number, marker=_COMMENT_MARKER)
    return state


def run(args: argparse.Namespace, *, github: ForgePort, policy: IndexPolicy) -> ExitCode:
    """`indexbot governance-check --pr-number <n>` entry point. See module
    docstring for the pipeline."""
    pr_number = cast(int, args.pr_number)
    info = github.get_pull_request_info(pr_number)
    change_class = classify_pull_request(info, github, policy=policy)
    state = gate_pull_request(info, change_class, github, policy=policy)
    write_ci_output("disposition", state)
    return ExitCode.OK
