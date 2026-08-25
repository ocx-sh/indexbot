"""`indexbot label-failed-run` — the privileged half of the "label a fork PR
whose checks failed" lane (ADR-6 FP-8; WP5-C).

Mirrors `tests/cli/test_governance_poll.py`'s house style: exercise `run`
against `FakeGitHub`, never respx — the SHA -> PR resolution and exact-match
filtering are `ForgePort.find_pull_request_by_head_sha`'s own contract,
covered against real payload shapes in `tests/test_github_api.py` and
`tests/adapters/test_gitlab_api.py`.
"""

from __future__ import annotations

import argparse

import pytest

from ocx_indexbot.cli import label_failed_run
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import PullRequestHeadMatch
from tests.fakes import FakeGitHub


def _run(github: FakeGitHub, head_sha: str | None) -> ExitCode:
    return label_failed_run.run(argparse.Namespace(head_sha=head_sha), github=github)


def test_add_arguments_head_sha_is_optional() -> None:
    """`--head-sha` must parse absent — `run` is what falls back to
    `$GITHUB_SHA`/`$CI_COMMIT_SHA`, not argparse."""
    parser = argparse.ArgumentParser()
    label_failed_run.add_arguments(parser)
    assert parser.parse_args([]).head_sha is None
    assert parser.parse_args(["--head-sha", "deadbeef"]).head_sha == "deadbeef"


def test_no_pull_request_has_this_head_is_a_clean_no_op() -> None:
    """A moved head (or a commit nothing is open against) is not an error —
    see `ports.ForgePort.find_pull_request_by_head_sha`'s docstring."""
    github = FakeGitHub()

    assert _run(github, "deadbeef") == ExitCode.OK
    assert github.labels == {}


def test_same_repo_pull_request_is_not_labeled() -> None:
    """ADR-6 FP-8 scopes `checks-failed` labeling to fork PRs only — a
    same-repository PR's failing checks are already visible to every
    maintainer with push access."""
    github = FakeGitHub(head_sha_lookup={"deadbeef": PullRequestHeadMatch(number=9, is_fork=False)})

    assert _run(github, "deadbeef") == ExitCode.OK
    assert github.labels == {}


def test_fork_pull_request_is_labeled_checks_failed() -> None:
    github = FakeGitHub(head_sha_lookup={"deadbeef": PullRequestHeadMatch(number=9, is_fork=True)})

    assert _run(github, "deadbeef") == ExitCode.OK
    assert github.labels[9] == ["checks-failed"]


def test_explicit_head_sha_wins_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "wrong-sha")
    github = FakeGitHub(head_sha_lookup={"deadbeef": PullRequestHeadMatch(number=9, is_fork=True)})

    assert _run(github, "deadbeef") == ExitCode.OK
    assert github.labels[9] == ["checks-failed"]


def test_falls_back_to_github_sha_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    monkeypatch.delenv("CI_COMMIT_SHA", raising=False)
    github = FakeGitHub(head_sha_lookup={"deadbeef": PullRequestHeadMatch(number=9, is_fork=True)})

    assert _run(github, None) == ExitCode.OK
    assert github.labels[9] == ["checks-failed"]


def test_falls_back_to_ci_commit_sha_when_github_sha_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GitLab equivalent of the same fallback — a hand-rolled GitLab
    pipeline sets `$CI_COMMIT_SHA`, never `$GITHUB_SHA`."""
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setenv("CI_COMMIT_SHA", "deadbeef")
    github = FakeGitHub(head_sha_lookup={"deadbeef": PullRequestHeadMatch(number=9, is_fork=True)})

    assert _run(github, None) == ExitCode.OK
    assert github.labels[9] == ["checks-failed"]


def test_missing_head_sha_and_environment_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv("CI_COMMIT_SHA", raising=False)

    with pytest.raises(ValidationError, match="--head-sha is required"):
        _run(FakeGitHub(), None)
