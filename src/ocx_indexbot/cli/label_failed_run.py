"""`indexbot label-failed-run --head-sha <sha>` — the privileged half of the
"label a fork PR whose checks failed" lane (ADR-6 FP-8 spam posture; WP5-C).

Folds what `.github/workflows/pr-checks-label.yml` currently does as three
`gh api`/`jq` calls into one command: resolve a completed run's head commit
back to the pull request it belongs to (`ForgePort.find_pull_request_by_head_sha`),
apply FP-8's fork-only scope, and — only then — add the `checks-failed` label
(`ForgePort.add_labels`) that `indexbot stale` later acts on.

**Trust boundary**, carried forward verbatim from the workflow this replaces:
this command never checks out any commit, fork-authored or otherwise, and
never runs PR-controlled code. `--head-sha` is metadata about a *completed*
run (GitHub's `workflow_run` event, or whatever a hand-rolled GitLab schedule
resolves it to) — a fact the caller is trusted to hand over, not content this
process reads or executes. The GitHub trigger that hands it over
(`workflow_run` on `workflows: ["validate"]`, `types: [completed]`) runs with
base-repository privileges regardless of who authored the workflow that just
completed, which is exactly why no repository content is ever checked out
here — see `ports.ForgePort.find_pull_request_by_head_sha`'s docstring for why
the lookup itself must filter to an exact head-sha match rather than trust the
API's raw association list.

**FP-8 scope: fork PRs only.** A same-repository PR's failing checks are
already visible to every maintainer with push access; labeling it would add
noise the label was never meant to carry, and — since `indexbot stale` acts on
this label — would put a same-repo PR on a stale-close clock nobody asked for.
`PullRequestHeadMatch.is_fork` is exactly that ADR-6 FP-8 test, computed once
in the port so this module never re-derives forge-specific "is this a fork"
logic.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Final, cast

from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode

if TYPE_CHECKING:
    import argparse

    from ocx_indexbot.ports import ForgePort

_LABEL: Final[str] = "checks-failed"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `label-failed-run`'s CLI surface.

    `--head-sha` is optional on the parser itself — `run` below falls back to
    `$GITHUB_SHA`/`$CI_COMMIT_SHA` when it is omitted, so a hand-rolled
    pipeline that already runs this command with the right commit checked out
    (or named by its own runner variable) needs no `${{ }}`/`$CI_*`
    interpolation of its own to invoke it correctly.
    """
    parser.add_argument(
        "--head-sha",
        default=None,
        help="the completed run's head commit (falls back to $GITHUB_SHA / $CI_COMMIT_SHA)",
    )


def _resolve_head_sha(args: argparse.Namespace) -> str:
    explicit = cast("str | None", args.head_sha)
    if explicit:
        return explicit
    from_env = os.environ.get("GITHUB_SHA") or os.environ.get("CI_COMMIT_SHA")
    if from_env:
        return from_env
    raise ValidationError(
        "label-failed-run: --head-sha is required (or set $GITHUB_SHA / $CI_COMMIT_SHA)"
    )


def run(args: argparse.Namespace, *, github: ForgePort) -> ExitCode:
    """`indexbot label-failed-run` entry point. See module docstring for the
    trust boundary and the FP-8 scoping rule this enforces."""
    head_sha = _resolve_head_sha(args)
    match = github.find_pull_request_by_head_sha(head_sha)
    if match is None:
        print(
            f"label-failed-run: no open pull request has {head_sha} as its current head "
            "— nothing to label.",
            file=sys.stderr,
        )
        return ExitCode.OK
    if not match.is_fork:
        print(
            f"label-failed-run: PR #{match.number} is not fork-authored — FP-8 scopes "
            "checks-failed labeling to fork PRs only.",
            file=sys.stderr,
        )
        return ExitCode.OK
    github.add_labels(match.number, [_LABEL])
    print(f"label-failed-run: labeled PR #{match.number} {_LABEL!r}", file=sys.stderr)
    return ExitCode.OK
