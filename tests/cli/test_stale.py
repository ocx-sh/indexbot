"""`indexbot stale` — first-party replacement for `actions/stale`
(ADR-6 FP-8 spam posture, second half; WP5-C).

Same house style as `tests/cli/test_governance_poll.py`: exercise `run`
against `FakeGitHub`/`FixedClock`, one behavior per named test. The default
`FixedClock` instant is `2026-07-17T00:00:00Z`; every `updated_at` below is
chosen relative to that so the day-count arithmetic in the assertion is
obvious from the docstring alone rather than from re-deriving it.
"""

from __future__ import annotations

import argparse

import pytest

from ocx_indexbot.cli import stale
from ocx_indexbot.errors import TransientError, ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import PullRequestInfo
from tests.fakes import FakeGitHub, FixedClock

_CHECKS_FAILED = "checks-failed"
_CHECKS_FAILED_STALE = "checks-failed-stale"


def _pr(number: int, *, updated_at: str, labels: tuple[str, ...]) -> PullRequestInfo:
    return PullRequestInfo(
        number=number,
        base_sha="base",
        head_sha="head",
        changed_paths=(),
        updated_at=updated_at,
        labels=labels,
    )


def _run(github: FakeGitHub, *, dry_run: bool = False) -> ExitCode:
    return stale.run(argparse.Namespace(dry_run=dry_run), github=github, clock=FixedClock())


def test_add_arguments_dry_run_defaults_false() -> None:
    parser = argparse.ArgumentParser()
    stale.add_arguments(parser)
    assert parser.parse_args([]).dry_run is False
    assert parser.parse_args(["--dry-run"]).dry_run is True


def test_an_empty_index_is_a_clean_no_op() -> None:
    assert _run(FakeGitHub()) == ExitCode.OK


def test_pr_without_checks_failed_label_is_untouched() -> None:
    """`only-labels: checks-failed` in `stale.yml`'s terms — a PR this lane
    was never told about must never be touched, let alone closed."""
    github = FakeGitHub(pull_request_info={1: _pr(1, updated_at="2026-01-01T00:00:00Z", labels=())})

    assert _run(github) == ExitCode.OK
    assert github.labels == {}
    assert github.closed_pull_requests == set()


def test_recently_active_checks_failed_pr_is_not_yet_stale() -> None:
    """7 days of no activity, under the 14-day `days-before-stale` threshold."""
    github = FakeGitHub(
        pull_request_info={1: _pr(1, updated_at="2026-07-10T00:00:00Z", labels=(_CHECKS_FAILED,))}
    )

    assert _run(github) == ExitCode.OK
    assert github.labels == {}
    assert github.comments == {}


def test_old_checks_failed_pr_is_marked_stale() -> None:
    """46 days of no activity clears the 14-day threshold: the stale label is
    added and the stale notice is posted exactly once."""
    github = FakeGitHub(
        pull_request_info={1: _pr(1, updated_at="2026-06-01T00:00:00Z", labels=(_CHECKS_FAILED,))}
    )

    assert _run(github) == ExitCode.OK
    assert github.labels[1] == [_CHECKS_FAILED_STALE]
    (body,) = github.comments[1].values()
    assert "no activity for 14 days" in body


def test_dry_run_reports_would_mark_stale_without_writing() -> None:
    github = FakeGitHub(
        pull_request_info={1: _pr(1, updated_at="2026-06-01T00:00:00Z", labels=(_CHECKS_FAILED,))}
    )

    assert _run(github, dry_run=True) == ExitCode.OK
    assert github.labels == {}
    assert github.comments == {}


def test_stale_pr_with_recent_activity_is_not_yet_closed() -> None:
    """5 days since it was marked stale, under the 7-day `days-before-close`
    threshold — the label update itself counts as the activity being timed."""
    labels = (_CHECKS_FAILED, _CHECKS_FAILED_STALE)
    github = FakeGitHub(
        pull_request_info={1: _pr(1, updated_at="2026-07-12T00:00:00Z", labels=labels)}
    )

    assert _run(github) == ExitCode.OK
    assert github.closed_pull_requests == set()


def test_old_stale_pr_is_closed() -> None:
    """46 days since the last update clears the 7-day `days-before-close`
    threshold too: the close notice is posted and the PR is closed."""
    labels = (_CHECKS_FAILED, _CHECKS_FAILED_STALE)
    github = FakeGitHub(
        pull_request_info={1: _pr(1, updated_at="2026-06-01T00:00:00Z", labels=labels)}
    )

    assert _run(github) == ExitCode.OK
    assert github.closed_pull_requests == {1}
    (body,) = github.comments[1].values()
    assert "no activity for 21 days" in body


def test_dry_run_reports_would_close_without_writing() -> None:
    labels = (_CHECKS_FAILED, _CHECKS_FAILED_STALE)
    github = FakeGitHub(
        pull_request_info={1: _pr(1, updated_at="2026-06-01T00:00:00Z", labels=labels)}
    )

    assert _run(github, dry_run=True) == ExitCode.OK
    assert github.closed_pull_requests == set()
    assert github.comments == {}


def test_one_bad_pull_request_never_ends_the_sweep(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A poller that aborted on the first failure would leave every later
    stale-eligible PR untouched until the next scheduled run. The failure is
    reported and the sweep continues; the run still exits non-zero."""
    github = FakeGitHub(
        pull_request_info={
            1: _pr(1, updated_at="2026-06-01T00:00:00Z", labels=(_CHECKS_FAILED,)),
            2: _pr(2, updated_at="2026-06-01T00:00:00Z", labels=(_CHECKS_FAILED,)),
        }
    )
    original = github.get_pull_request_info

    def _explode(pr_number: int) -> PullRequestInfo:
        if pr_number == 1:
            raise TransientError("GitHub API rate limit exceeded")
        return original(pr_number)

    github.get_pull_request_info = _explode  # pyright: ignore[reportAttributeAccessIssue]

    result = _run(github)

    assert result == ExitCode.TRANSIENT
    assert github.labels[2] == [_CHECKS_FAILED_STALE], "the PR after the failure was still handled"
    assert "#1" in capsys.readouterr().err


def test_an_unexpected_error_costs_one_pull_request_not_the_sweep(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Blast radius is one pull request whatever the layer below raises —
    the same guard `cli/governance_poll.py` carries, and for the same reason:
    a raw, unrecognized exception from one PR must not end the run."""
    github = FakeGitHub(
        pull_request_info={
            1: _pr(1, updated_at="2026-06-01T00:00:00Z", labels=(_CHECKS_FAILED,)),
        }
    )
    github.pull_request_info[2] = _BOOM  # pyright: ignore[reportArgumentType]

    result = _run(github)

    assert result == ExitCode.VALIDATION_FAILURE
    assert github.labels[1] == [_CHECKS_FAILED_STALE], "the healthy PR was still handled"
    assert "#2: AttributeError" in capsys.readouterr().err


def test_validation_error_maps_to_the_validation_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    github = FakeGitHub(
        pull_request_info={1: _pr(1, updated_at="2026-06-01T00:00:00Z", labels=(_CHECKS_FAILED,))}
    )

    def _explode(pr_number: int) -> PullRequestInfo:
        del pr_number
        raise ValidationError("malformed pull request payload")

    github.get_pull_request_info = _explode  # pyright: ignore[reportAttributeAccessIssue]

    assert _run(github) == ExitCode.VALIDATION_FAILURE
    assert "malformed pull request payload" in capsys.readouterr().err


_BOOM = object()
"""Stands in for a `PullRequestInfo` and raises `AttributeError` on first
attribute read — a defect in this bot, not a forge failure."""
