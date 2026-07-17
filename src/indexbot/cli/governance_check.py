"""`indexbot governance-check` — sets the `governance/review-required`
required commit status (CONTRACTS.md §12; ADR-4 BD-5).

Re-derives the PR's classification via `cli/classify_pr.classify_pull_request`
rather than reading a label back (no `GitHubPort.get_labels`-shaped method
exists, and `.github/workflows/validate.yml` invokes this as its own
process, separate from `indexbot classify-pr` — see `classify_pr.py`'s
module docstring for why re-deriving through the shared pure function is the
single-source-of-truth option, CONTRACTS.md §13 item 6).

**Deviation from ADR-4 BD-5's fuller "green for refresh PRs once
schema-validate is also green" condition — flagged here, not silently
applied.** `schema-validate-pr` and `governance-gate` are two separate
workflow runs of `validate.yml` (triggered by `pull_request` and
`pull_request_target` respectively) — a plain `needs:`/`if:` job-graph gate
cannot cross between them (CONTRACTS.md §13 item 6's own proposed default
does not actually work for that reason), and polling the Checks API for a
sibling run's result is out of this stage's assigned scope. This module's
status is therefore `success` for `refresh` classification alone;
`schema-validate-pr` remains a *separate* required status check under branch
protection (`validate.yml`'s top-of-file commentary), so a refresh PR still
cannot merge unless both checks are independently green — only the "does
`governance-check` itself also confirm schema-validate's result" refinement
is deferred, not the merge-gating behavior. Confirm before treating this as
final.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

from indexbot.cli.classify_pr import classify_pull_request
from indexbot.exit_codes import ExitCode

if TYPE_CHECKING:
    import argparse

    from indexbot.model import CommitStatusState
    from indexbot.ports import GitHubPort

_STATUS_CONTEXT: Final[str] = "governance/review-required"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `governance-check`'s CLI surface — `--pr-number`
    mirrors `cli/classify_pr.py`'s (trusted Actions-context value, no
    env-var-indirection discipline needed)."""
    parser.add_argument("--pr-number", type=int, required=True, help="pull request number to gate")


def _disposition(change_class: str) -> tuple[CommitStatusState, str]:
    """`(state, description)` per ADR-4 BD-5's green/red rule: green only
    for `refresh`; every other classification (`new-package`,
    `human-review-required`) is `pending` until a human approves — never
    `failure`, since nothing has actually gone wrong, the PR just needs
    review before it may auto-merge."""
    if change_class == "refresh":
        return "success", "refresh: no governance review required"
    return "pending", f"{change_class}: awaiting human review"


def run(args: argparse.Namespace, *, github: GitHubPort) -> ExitCode:
    """`indexbot governance-check --pr-number <n>` entry point. See module
    docstring for the pipeline and its documented deviation from ADR-4
    BD-5's fuller cross-job condition."""
    pr_number = cast(int, args.pr_number)
    info = github.get_pull_request_info(pr_number)
    change_class = classify_pull_request(info, github)

    state, description = _disposition(change_class)
    github.set_commit_status(
        info.head_sha, context=_STATUS_CONTEXT, state=state, description=description
    )
    return ExitCode.OK
