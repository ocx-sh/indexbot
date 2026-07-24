"""`indexbot governance-check` — sets the `governance/review-required`
required commit status, plus G-19/G-20 reviewer assignment (fork-PR announce
revamp, owner-confirmed decision set 2026-07-18).

Re-derives the PR's classification via `cli/classify_pr.classify_pull_request`
rather than reading a label back (no `GitHubPort.get_labels`-shaped method
exists, and `.github/workflows/validate.yml` invokes this as its own
process, separate from `indexbot classify-pr` — single-source-of-truth via
the shared pure function, not a second hand-rolled diff walk).

Disposition:

- **Machine lane** (`refresh`): green (`success`) requires the PR author's
  `github_id` to appear in `owners[]` of *every* touched package root — read
  from the **base** ref only (`GitHubPort.get_file_contents`, never the PR
  head; the same untrusted-head-content trust boundary
  `cli/classify_pr.py`'s module docstring already documents for
  `governance-gate`'s `pull_request_target` context) — this is G-19. A
  refresh-classified PR whose author does not own every touched package
  falls back to the human lane below (`pending` + reviewers + comment)
  rather than merging unreviewed.
- **Human lane** (`new-package`/`human-review-required`, or a refresh PR that
  failed G-19): always `pending` + reviewers assigned from committed
  `.github/maintainers.yml` (read from the base ref) + one idempotent
  comment — G-20. Never `failure`: nothing has actually gone wrong, the PR
  just needs a human before it may merge.

Reviewers are every `.github/maintainers.yml` entry's `github` login, minus
the PR author (self-review carve-out — GitHub's API itself rejects
assigning a PR's own author as one of their own reviewers). The comment uses
a hidden HTML marker (`<!-- indexbot:governance -->`) so a later
`governance-check` run on the same PR updates the existing comment in place
rather than reposting on every re-run.

Writes the resulting commit-status state (`"success"` or `"pending"`) to
`$GITHUB_OUTPUT` as `disposition` — `.github/workflows/validate.yml`'s
`governance-gate` job reads this back (`steps.governance_check.outputs.
disposition`) to decide whether to arm auto-merge, rather than re-reading the
label `indexbot classify-pr` applied (G-19 requires the *ownership-checked*
disposition, not just the raw `refresh`/`new-package`/`human-review-required`
classification).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Final, cast

from indexbot.cli.classify_pr import classify_pull_request
from indexbot.core.maintainers import parse_maintainers
from indexbot.core.validate_entry import parse_package_root
from indexbot.errors import ValidationError
from indexbot.exit_codes import ExitCode

from ._common import write_github_output

if TYPE_CHECKING:
    import argparse

    from indexbot.model import CommitStatusState, Owner, PullRequestInfo
    from indexbot.ports import GitHubPort

_STATUS_CONTEXT: Final[str] = "governance/review-required"
_MAINTAINERS_PATH: Final[str] = ".github/maintainers.yml"
_COMMENT_MARKER: Final[str] = "<!-- indexbot:governance -->"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `governance-check`'s CLI surface — `--pr-number`
    mirrors `cli/classify_pr.py`'s (trusted Actions-context value, no
    env-var-indirection discipline needed)."""
    parser.add_argument("--pr-number", type=int, required=True, help="pull request number to gate")


def _is_package_root_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == "p" and parts[2].endswith(".json")


def _author_owns_every_touched_package(info: PullRequestInfo, github: GitHubPort) -> bool:
    """G-19: the PR author's `github_id` must appear in `owners[]` of every
    touched `p/<namespace>/<package>.json` root, read from the base ref
    (never the PR head)."""
    root_paths = [path for path in info.changed_paths if _is_package_root_path(path)]
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


def _disposition(change_class: str, *, author_is_owner: bool) -> tuple[CommitStatusState, str]:
    """`(state, description)`: green only for `refresh` classification AND
    G-19's author-ownership check; every other outcome is `pending` until a
    human reviews — never `failure`."""
    if change_class == "refresh":
        if author_is_owner:
            return "success", "refresh: PR author owns every touched package, no review required"
        return "pending", "refresh: PR author does not own every touched package (G-19)"
    return "pending", f"{change_class}: awaiting human review"


def _committed_maintainers(github: GitHubPort, base_sha: str) -> tuple[Owner, ...]:
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


def _assign_reviewers_and_comment(
    github: GitHubPort, info: PullRequestInfo, *, reason: str
) -> None:
    """G-20: reviewers from committed `.github/maintainers.yml` (base ref),
    minus the PR author (self-review carve-out), plus one idempotent
    comment explaining why review is needed."""
    maintainers = _committed_maintainers(github, info.base_sha)
    logins = [
        maintainer.github for maintainer in maintainers if maintainer.github != info.author_login
    ]
    if logins:
        github.request_reviewers(info.number, logins)
    github.create_comment(
        info.number,
        f"{_COMMENT_MARKER}\nThis PR requires human review: {reason}.",
        marker=_COMMENT_MARKER,
    )


def run(args: argparse.Namespace, *, github: GitHubPort) -> ExitCode:
    """`indexbot governance-check --pr-number <n>` entry point. See module
    docstring for the pipeline."""
    pr_number = cast(int, args.pr_number)
    info = github.get_pull_request_info(pr_number)
    change_class = classify_pull_request(info, github)

    author_is_owner = change_class == "refresh" and _author_owns_every_touched_package(info, github)
    state, description = _disposition(change_class, author_is_owner=author_is_owner)
    github.set_commit_status(
        info.head_sha, context=_STATUS_CONTEXT, state=state, description=description
    )
    if state != "success":
        _assign_reviewers_and_comment(github, info, reason=description)
    write_github_output("disposition", state)
    return ExitCode.OK
