"""`indexbot governance-gate --pr <n>` — the single-PR body
`cli/governance_poll.py`'s sweep now shares
(`governance_gate.gate_pull_request_and_sync_auto_merge`).

These tests pin the CLI surface, that a green PR arms auto-merge bound to
its own head, that a non-green PR withdraws whatever was armed (idempotently
— never an error on a PR that was never armed at all), and that the poll
lane and this one-PR gate reach the identical disposition for the same PR —
the entire reason the two share one implementation rather than two.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ocx_indexbot.cli import governance_gate, governance_poll
from ocx_indexbot.core.validate_entry import serialize_package_root
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import Owner, PackageRoot, PullRequestInfo, TagEntry
from tests.fakes import FakeGitHub, make_policy

_OWNER = Owner(login="alice", id=1)
_OTHER_OWNER = Owner(login="bob", id=2)
_BASE = "base-sha"
_HEAD = "head-sha"
_ROOT_PATH = "p/kitware/cmake.json"
_STATUS_CONTEXT = "governance/review-required"
_MAINTAINERS_PATH = ".github/maintainers.yml"
_MAINTAINERS_YML = b"maintainers:\n  - login: carol\n    id: 99\n"


@pytest.fixture(autouse=True)
def _github_output(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """`run()` unconditionally writes the `disposition` `$GITHUB_OUTPUT` entry
    (see module docstring) — every test needs a target file for that write,
    not just the ones asserting on its contents."""
    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    return output_file


def _root(*, tag_digest: str = "a", owners: tuple[Owner, ...] = (_OWNER,)) -> PackageRoot:
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


def _github(
    *,
    pr_number: int = 1,
    base: PackageRoot | None,
    head: PackageRoot,
    head_sha: str = _HEAD,
    author_login: str = "alice",
    author_id: int = 1,
    maintainers: bytes | None = _MAINTAINERS_YML,
) -> FakeGitHub:
    files: dict[tuple[str, str], bytes] = {(_ROOT_PATH, head_sha): serialize_package_root(head)}
    if base is not None:
        files[(_ROOT_PATH, _BASE)] = serialize_package_root(base)
    if maintainers is not None:
        files[(_MAINTAINERS_PATH, _BASE)] = maintainers
    info = PullRequestInfo(
        number=pr_number,
        base_sha=_BASE,
        head_sha=head_sha,
        changed_paths=(_ROOT_PATH,),
        author_login=author_login,
        author_id=author_id,
    )
    return FakeGitHub(files=files, pull_request_info={pr_number: info})


def _args(pr: int = 1, **overrides: object) -> argparse.Namespace:
    """`governance-gate`'s full parsed surface, so a test never accidentally
    asserts against a Namespace narrower than argparse builds."""
    fields: dict[str, object] = {
        "pr": pr,
        "no_arm": False,
        "arm_only": False,
        "disposition": "",
        "head_sha": "",
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


def _run(github: FakeGitHub, pr: int = 1, **overrides: object) -> ExitCode:
    return governance_gate.run(_args(pr, **overrides), github=github, policy=make_policy())


def test_add_arguments_registers_pr() -> None:
    parser = argparse.ArgumentParser()
    governance_gate.add_arguments(parser)
    args = parser.parse_args(["--pr", "7"])
    assert args.pr == 7


def test_add_arguments_requires_pr() -> None:
    parser = argparse.ArgumentParser()
    governance_gate.add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_green_pr_arms_auto_merge_bound_to_its_own_head() -> None:
    github = _github(base=_root(tag_digest="a"), head=_root(tag_digest="b"))

    result = _run(github)

    assert result is ExitCode.OK
    assert github.labels[1] == ["refresh"]
    assert github.statuses[_HEAD] == [
        (
            _STATUS_CONTEXT,
            "success",
            "refresh: PR author owns every touched package, no review required",
        )
    ]
    assert github.auto_merge_enabled == {1}
    assert github.auto_merge_head_sha[1] == _HEAD


def test_human_lane_pr_never_arms_and_withdrawing_nothing_is_not_an_error() -> None:
    """A PR that was never armed must withdraw as a cheap no-op, not an
    error — the ordinary human-lane case (a brand-new package here)."""
    github = _github(base=None, head=_root())

    result = _run(github)

    assert result is ExitCode.OK
    assert github.labels[1] == ["new-package"]
    assert github.auto_merge_enabled == set()


def test_a_regressed_pr_has_its_earlier_arm_withdrawn() -> None:
    """A PR armed as machine-lane, then pushed a revision no longer owned by
    its author, must have that arming taken back — matching
    `governance.yml`'s "Withdraw auto-merge — human lane" step exactly."""
    before = _root(tag_digest="a")
    github = _github(base=before, head=_root(tag_digest="b"))
    assert _run(github) is ExitCode.OK
    assert github.auto_merge_enabled == {1}

    # A later push moves ownership away from the PR's author — reclassifies
    # to "human-review-required" on the next run.
    github.pull_request_info[1] = PullRequestInfo(
        number=1,
        base_sha=_BASE,
        head_sha="head-2",
        changed_paths=(_ROOT_PATH,),
        author_login="alice",
        author_id=1,
    )
    github.files[(_ROOT_PATH, "head-2")] = serialize_package_root(
        _root(tag_digest="c", owners=(_OTHER_OWNER,))
    )

    assert _run(github) is ExitCode.OK
    assert github.auto_merge_enabled == set(), "the earlier arm must be withdrawn"


def test_disposition_is_published_as_a_ci_output(_github_output: Path) -> None:
    github = _github(base=_root(tag_digest="a"), head=_root(tag_digest="b"))

    _run(github)

    outputs = _github_output.read_text(encoding="utf-8")
    assert "disposition" in outputs
    assert "success" in outputs


def test_the_poll_lane_and_the_single_pr_gate_reach_the_same_disposition() -> None:
    """The whole point of sharing `gate_pull_request_and_sync_auto_merge`:
    `governance-poll`'s sweep and `governance-gate --pr` must never diverge
    on one merge request's outcome, classification, or auto-merge arming."""
    policy = make_policy()
    before, head = _root(tag_digest="a"), _root(tag_digest="b")
    via_gate = _github(base=before, head=head)
    via_poll = _github(base=before, head=head)

    governance_gate.run(_args(1), github=via_gate, policy=policy)
    governance_poll.run(argparse.Namespace(), github=via_poll, policy=policy)

    assert via_gate.labels[1] == via_poll.labels[1]
    assert via_gate.statuses[_HEAD] == via_poll.statuses[_HEAD]
    assert via_gate.auto_merge_enabled == via_poll.auto_merge_enabled == {1}
    assert via_gate.auto_merge_head_sha == via_poll.auto_merge_head_sha


# --- --no-arm / --arm-only: the two-job split GitHub's lane renders ----------


def test_add_arguments_registers_the_two_phase_flags() -> None:
    parser = argparse.ArgumentParser()
    governance_gate.add_arguments(parser)

    default = parser.parse_args(["--pr", "7"])
    assert (default.no_arm, default.arm_only, default.disposition, default.head_sha) == (
        False,
        False,
        "",
        "",
    )

    armed = parser.parse_args(
        ["--pr", "7", "--arm-only", "--disposition", "success", "--head-sha", "abc"]
    )
    assert (armed.arm_only, armed.disposition, armed.head_sha) == (True, "success", "abc")


def test_no_arm_and_arm_only_are_mutually_exclusive() -> None:
    """One invocation is either the gate half or the arm half. "Both" would be
    a job that classifies and then arms on a disposition it was handed rather
    than the one it computed — two answers, one PR."""
    parser = argparse.ArgumentParser()
    governance_gate.add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--pr", "7", "--no-arm", "--arm-only"])


def test_no_arm_gates_and_publishes_but_arms_nothing(_github_output: Path) -> None:
    """What lets `governance.yml`'s gate job stay at `contents: read`: a PR
    that would otherwise arm still gets classified, labeled and gated, and the
    disposition still reaches the second job — only the auto-merge write is
    withheld."""
    github = _github(base=_root(tag_digest="a"), head=_root(tag_digest="b"))

    assert _run(github, no_arm=True) is ExitCode.OK

    assert github.labels[1] == ["refresh"]
    assert github.statuses[_HEAD][0][1] == "success"
    assert "success" in _github_output.read_text(encoding="utf-8")
    assert github.auto_merge_enabled == set(), "--no-arm must not arm"


def test_arm_only_arms_from_a_handed_disposition_bound_to_the_handed_head() -> None:
    """The arm job classifies nothing: it replays the gate's decision, bound
    to the revision the event delivered."""
    github = _github(base=_root(tag_digest="a"), head=_root(tag_digest="b"))

    governance_gate.sync_auto_merge(1, github, disposition="success", head_sha=_HEAD)

    assert github.auto_merge_enabled == {1}
    assert github.auto_merge_head_sha[1] == _HEAD
    assert github.labels == {}, "the arm half must write no label"
    assert github.statuses == {}, "the arm half must publish no commit status"


def test_arm_only_withdraws_on_an_empty_disposition() -> None:
    """The fail-closed case the whole two-job split exists for. A gate that
    ERRORS publishes no disposition at all, and `if: ${{ !cancelled() }}` still
    runs the arm job — which must read that emptiness as "not machine-lane" and
    take back whatever an earlier run armed, never leave it standing on an
    evaluation that never finished."""
    github = _github(base=_root(tag_digest="a"), head=_root(tag_digest="b"))
    governance_gate.sync_auto_merge(1, github, disposition="success", head_sha=_HEAD)
    assert github.auto_merge_enabled == {1}

    governance_gate.sync_auto_merge(1, github, disposition="", head_sha="")

    assert github.auto_merge_enabled == set()


def test_arm_only_withdraws_on_a_pending_disposition() -> None:
    """Anything that is not literally `success` withdraws — a human-lane PR
    is not a special case, it is the default branch of the same rule."""
    github = _github(base=None, head=_root())

    governance_gate.sync_auto_merge(1, github, disposition="pending", head_sha=_HEAD)

    assert github.auto_merge_enabled == set()


def test_the_split_and_the_single_call_reach_the_same_arm() -> None:
    """`--no-arm` then `--arm-only` is the same outcome as one default run.
    The GitHub lane splits the call for fail-closed withdrawal, not to reach a
    different decision than GitLab's poller does in one process."""
    policy = make_policy()
    before, head = _root(tag_digest="a"), _root(tag_digest="b")
    one_call = _github(base=before, head=head)
    two_jobs = _github(base=before, head=head)

    governance_gate.run(_args(1), github=one_call, policy=policy)
    governance_gate.run(_args(1, no_arm=True), github=two_jobs, policy=policy)
    governance_gate.sync_auto_merge(1, two_jobs, disposition="success", head_sha=_HEAD)

    assert one_call.labels == two_jobs.labels
    assert one_call.statuses == two_jobs.statuses
    assert one_call.auto_merge_enabled == two_jobs.auto_merge_enabled == {1}
    assert one_call.auto_merge_head_sha == two_jobs.auto_merge_head_sha
