"""`indexbot stale` — first-party replacement for GitHub's third-party
`actions/stale` (ADR-6 FP-8 spam posture, second half; WP5-C).

`.github/workflows/stale.yml` today delegates the "close a fork PR whose
checks failed and nobody followed up" lane to `actions/stale`; GitLab has no
equivalent action at all. This module is the same sweep, implemented once, so
it runs identically on both forges: list every open pull request, and for
each one already carrying `label-failed-run`'s `checks-failed` label, decide
stale / warn / close purely from its own `updated_at` and label set.

**Thresholds, labels and messages are package constants, not deployment
policy.** `stale.yml`'s current `actions/stale` configuration carries no
`{{placeholder}}` anywhere in `ci/templates/github/stale.yml` — every value
below (`only-labels`, `days-before-stale`, `days-before-close`, the two label
names, both messages) is already the same fixed literal for every deployment
that renders that template. `.github/index-policy.json` therefore gains no
new keys for this: promoting a value to policy is for something a *different*
index deployment might legitimately want to change (`core/policy.py`'s own
`name`/`registry_hosts`/schedules), not for values the generator itself never
varied.

**Never touches issues.** `stale.yml` disables issue staling entirely
(`days-before-issue-stale: -1`, `days-before-issue-close: -1`); this command
owns pull-request triage only, exactly like `pr-checks-label.yml`'s labeling
half it feeds — the anomaly-tracking issues `reconcile`/`validate` file are a
different lane with a different owner.

**One PR's failure never ends the sweep**, for the same reason
`cli/governance_poll.py`'s docstring gives: a forge hiccup on one PR's comment
or close call must not leave every other stale-eligible PR untouched until the
next scheduled run notices. Each PR is handled independently and the run
exits with the worst code any one of them produced.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import TYPE_CHECKING, Final, cast

from ocx_indexbot.errors import IndexBotError
from ocx_indexbot.exit_codes import ExitCode

if TYPE_CHECKING:
    import argparse

    from ocx_indexbot.model import PullRequestInfo
    from ocx_indexbot.ports import ClockPort, ForgePort

_TRIGGER_LABEL: Final[str] = "checks-failed"
_STALE_LABEL: Final[str] = "checks-failed-stale"

_DAYS_BEFORE_STALE: Final[int] = 14
_DAYS_BEFORE_CLOSE: Final[int] = 7
"""Verbatim from `ci/templates/github/stale.yml`'s `actions/stale` step
(`days-before-stale: 14`, `days-before-close: 7`). `days-before-close` is
measured against the SAME `updated_at` re-read every sweep — marking a PR
stale is itself an update (a label add plus a comment), so the closing
countdown runs from that moment, not from the original last-human-activity
date. That is `actions/stale`'s own behavior, not an approximation of it: the
messages below already say "14 days" then "7 days", 21 total."""

_STALE_MARKER: Final[str] = "<!-- indexbot:stale -->"
_CLOSE_MARKER: Final[str] = "<!-- indexbot:stale-closed -->"

_STALE_MESSAGE: Final[str] = (
    "This pull request has failing checks (ADR-6 FP-8 spam posture) and has had no "
    "activity for 14 days. It will be closed in 7 days unless updated."
)
_CLOSE_MESSAGE: Final[str] = (
    "Closing this pull request: failing checks with no activity for 21 days (ADR-6 FP-8)."
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `stale`'s CLI surface — no scope to narrow (the
    sweep is always "every open PR", matching `governance-poll`'s own
    zero-argument convention), only whether it writes."""
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change; write nothing"
    )


def _age_days(updated_at: str, now: str) -> int:
    """Whole days between `updated_at` and `now`, both RFC 3339 —
    `actions/stale`'s own comparisons are day-granular, never finer."""
    return (datetime.fromisoformat(now) - datetime.fromisoformat(updated_at)).days


def _handle_one(info: PullRequestInfo, github: ForgePort, now: str, *, dry_run: bool) -> str:
    if _TRIGGER_LABEL not in info.labels:
        return f"no {_TRIGGER_LABEL!r} label, skipped"

    age = _age_days(info.updated_at, now)

    if _STALE_LABEL not in info.labels:
        if age < _DAYS_BEFORE_STALE:
            return f"{age}d since last activity, not yet stale"
        if dry_run:
            return f"would mark stale ({age}d since last activity)"
        github.add_labels(info.number, [_STALE_LABEL])
        github.create_comment(
            info.number, f"{_STALE_MARKER}\n{_STALE_MESSAGE}", marker=_STALE_MARKER
        )
        return f"marked stale ({age}d since last activity)"

    if age < _DAYS_BEFORE_CLOSE:
        return f"stale, {age}d since marked, not yet closing"
    if dry_run:
        return f"would close ({age}d since marked stale)"
    github.create_comment(info.number, f"{_CLOSE_MARKER}\n{_CLOSE_MESSAGE}", marker=_CLOSE_MARKER)
    github.close_pull_request(info.number)
    return f"closed ({age}d since marked stale)"


def run(args: argparse.Namespace, *, github: ForgePort, clock: ClockPort) -> ExitCode:
    """`indexbot stale [--dry-run]` entry point. See module docstring for the
    thresholds and why they are package constants rather than policy."""
    dry_run = cast(bool, args.dry_run)
    worst = ExitCode.OK
    now = clock.now_iso8601()
    numbers = github.list_open_pull_requests()
    print(f"stale: {len(numbers)} open pull request(s)", file=sys.stderr)
    for number in numbers:
        try:
            info = github.get_pull_request_info(number)
            outcome = _handle_one(info, github, now, dry_run=dry_run)
        except IndexBotError as exc:
            print(f"stale: #{number}: {exc}", file=sys.stderr)
            worst = max(worst, exc.exit_code)
        except Exception as exc:  # deliberate: one PR must never end the sweep
            print(f"stale: #{number}: {type(exc).__name__}: {exc}", file=sys.stderr)
            worst = max(worst, ExitCode.VALIDATION_FAILURE)
        else:
            print(f"stale: #{number}: {outcome}", file=sys.stderr)
    return worst
