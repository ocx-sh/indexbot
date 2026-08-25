"""`indexbot governance-poll` — the GitLab governance lane.

The poll lane exists because GitLab has no privileged merge-request trigger;
it must therefore decide *identically* to the GitHub lane and differ only in
when it runs and in who arms auto-merge. These tests say that in both
directions: the same disposition comes out, and the sweep survives one bad
merge request.
"""

from __future__ import annotations

import argparse

import pytest

from ocx_indexbot.cli import governance_poll
from ocx_indexbot.core.validate_entry import serialize_package_root
from ocx_indexbot.errors import TransientError, ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import Owner, PackageRoot, PullRequestInfo, TagEntry
from tests.fakes import FakeGitHub, make_policy

_OWNER = Owner(github="alice", github_id=1)
_MAINTAINERS_PATH = ".github/maintainers.yml"
_MAINTAINERS_YML = b"maintainers:\n  - github: carol\n    github_id: 99\n"
_STATUS_CONTEXT = "governance/review-required"


def _root(*, tag_digest: str, owners: tuple[Owner, ...] = (_OWNER,)) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/kitware/cmake",
        owners=owners,
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags={"1.0.0": TagEntry(content=f"sha256:{tag_digest * 64}", observed="T0")},
    )


def _refresh_mr(
    number: int, *, author_id: int = 1
) -> tuple[PullRequestInfo, dict[tuple[str, str], bytes]]:
    """One machine-lane merge request: a tag digest moved, nothing else."""
    path = "p/kitware/cmake.json"
    base, head = f"base-{number}", f"head-{number}"
    files: dict[tuple[str, str], bytes] = {
        (path, base): serialize_package_root(_root(tag_digest="a")),
        (path, head): serialize_package_root(_root(tag_digest="b")),
        (_MAINTAINERS_PATH, base): _MAINTAINERS_YML,
    }
    info = PullRequestInfo(
        number=number,
        base_sha=base,
        head_sha=head,
        changed_paths=(path,),
        author_login="alice",
        author_id=author_id,
    )
    return info, files


def _forge(*numbers: int, author_id: int = 1) -> FakeGitHub:
    files: dict[tuple[str, str], bytes] = {}
    infos: dict[int, PullRequestInfo] = {}
    for number in numbers:
        info, mr_files = _refresh_mr(number, author_id=author_id)
        infos[number] = info
        files.update(mr_files)
    return FakeGitHub(files=files, pull_request_info=infos)


def _run(github: FakeGitHub, **policy_overrides: object) -> ExitCode:
    return governance_poll.run(
        argparse.Namespace(), github=github, policy=make_policy(**policy_overrides)
    )


def test_add_arguments_declares_no_surface() -> None:
    """The sweep's scope is always "every open MR" — there is nothing to
    narrow, and a `--pr-number` here would just be `governance-check`."""
    parser = argparse.ArgumentParser()
    governance_poll.add_arguments(parser)
    assert parser.parse_args([]) == argparse.Namespace()


def test_gates_every_open_merge_request() -> None:
    github = _forge(1, 2, 3)

    assert _run(github) is ExitCode.OK

    for number in (1, 2, 3):
        assert github.statuses[f"head-{number}"] == [
            (
                _STATUS_CONTEXT,
                "success",
                "refresh: PR author owns every touched package, no review required",
            )
        ]
        assert github.labels[number] == ["refresh"]


def test_arms_auto_merge_only_for_the_green_ones() -> None:
    """The poller is the arming actor on GitLab — GitHub's `arm-auto-merge`
    job has no MR-driven counterpart there. It arms strictly on the gate's
    own disposition, exactly as that job does."""
    github = _forge(1)
    github.pull_request_info[2] = PullRequestInfo(
        number=2,
        base_sha="base-2",
        head_sha="head-2",
        changed_paths=("p/kitware/cmake.json",),
        author_login="mallory",
        author_id=404,
    )
    github.files[("p/kitware/cmake.json", "base-2")] = serialize_package_root(_root(tag_digest="a"))
    github.files[("p/kitware/cmake.json", "head-2")] = serialize_package_root(_root(tag_digest="b"))
    github.files[(_MAINTAINERS_PATH, "base-2")] = _MAINTAINERS_YML

    assert _run(github) is ExitCode.OK

    assert github.auto_merge_enabled == {1}, "MR 2 failed G-19 and must stay unarmed"


def test_auto_merge_never_arms_nothing() -> None:
    github = _forge(1, 2)

    assert _run(github, auto_merge="never") is ExitCode.OK

    assert github.auto_merge_enabled == set()
    assert github.comments.keys() == {1, 2}


def test_an_empty_index_is_a_clean_no_op() -> None:
    assert _run(FakeGitHub()) is ExitCode.OK


def test_one_bad_merge_request_never_ends_the_sweep(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A poller that aborted on the first failure would leave every later MR
    ungated until a human noticed. The failure is reported and the sweep
    continues; the run still exits non-zero."""
    github = _forge(1, 2, 3)
    original = github.get_pull_request_info

    def _explode(pr_number: int) -> PullRequestInfo:
        if pr_number == 2:
            raise TransientError("GitLab API rate limit exceeded")
        return original(pr_number)

    github.get_pull_request_info = _explode  # pyright: ignore[reportAttributeAccessIssue]

    result = _run(github)

    assert result is ExitCode.TRANSIENT
    assert github.auto_merge_enabled == {1, 3}, "the MRs either side of the failure were gated"
    assert "#2" in capsys.readouterr().err


def test_a_forge_timeout_scores_the_merge_request_retryable_not_invalid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sweep's blanket `except Exception` is a blast-radius guard, and it
    was also scoring every unclassified failure `VALIDATION_FAILURE`.

    A read timeout to the forge is not a verdict on the merge request. Exit 1
    says the branch is invalid; 75 says run me again — and only 75 makes the
    next tick a retry rather than a re-report. Production hit exactly this:
    `governance-poll: #10: ReadTimeout: The read operation timed out`, exit 1,
    against an announce that was fine. The fix is in `adapters/_http.py`, so
    what reaches here is already a `TransientError`; this test is the half
    that says the sweep then scores it correctly.
    """
    github = _forge(1, 2)
    original = github.get_pull_request_info

    def _time_out(pr_number: int) -> PullRequestInfo:
        if pr_number == 1:
            raise TransientError("GitLab API ReadTimeout for GET /projects/42: timed out")
        return original(pr_number)

    github.get_pull_request_info = _time_out  # pyright: ignore[reportAttributeAccessIssue]

    assert _run(github) is ExitCode.TRANSIENT
    assert github.auto_merge_enabled == {2}, "the merge request after the timeout was still gated"
    assert "ReadTimeout" in capsys.readouterr().err


def test_the_worst_exit_code_wins(capsys: pytest.CaptureFixture[str]) -> None:
    """Ordering by the `ExitCode` values themselves carries no meaning beyond
    "not zero" — but a sweep that hit both a validation failure and a
    transient must not report the milder one and read as retryable-only."""
    github = _forge(1, 2, 3)
    original = github.get_pull_request_info

    def _explode(pr_number: int) -> PullRequestInfo:
        if pr_number == 1:
            raise ValidationError("malformed base-ref root")
        if pr_number == 2:
            raise TransientError("rate limited")
        return original(pr_number)

    github.get_pull_request_info = _explode  # pyright: ignore[reportAttributeAccessIssue]

    assert _run(github) is ExitCode.TRANSIENT
    errors = capsys.readouterr().err
    assert "malformed base-ref root" in errors, "the milder failure is still reported"


def test_an_unexpected_error_costs_one_merge_request_not_the_sweep(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Blast radius is one merge request whatever the layer below raises.

    Scoping the guard to `IndexBotError` cost a real production sweep: a raw
    `httpx.HTTPStatusError` from one commit-status POST ended the run, leaving
    every later merge request ungated. The adapters wrap their own failures
    now, so this is the second line of defence — and it is not a quiet no-op,
    because the run still exits non-zero and names the type on stderr.
    """
    github = _forge(1, 2)
    github.pull_request_info[1] = _BOOM  # pyright: ignore[reportArgumentType]

    assert _run(github) == ExitCode.VALIDATION_FAILURE

    assert 2 in github.auto_merge_enabled, "the healthy merge request was still gated"
    err = capsys.readouterr().err
    assert "#1: AttributeError" in err


_BOOM = object()
"""Stands in for a `PullRequestInfo` and raises `AttributeError` on first
attribute read — a defect in this bot, not a forge failure."""


def test_the_arm_is_bound_to_the_revision_that_was_gated() -> None:
    """The whole point of `enable_auto_merge(head_sha=...)`: both forges take
    it as an optimistic-concurrency guard, so a push between the gate and the
    arm re-opens the question instead of merging unreviewed content. Nothing
    caught this reverting — mutating the argument to a constant left the suite
    green at 100% branch coverage, because the only assertion on the fake's
    record was in the fake's own tests."""
    github = _forge(1)

    assert _run(github) == ExitCode.OK

    assert github.auto_merge_head_sha[1] == github.pull_request_info[1].head_sha
