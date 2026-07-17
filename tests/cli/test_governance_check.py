from __future__ import annotations

import argparse

import pytest

from indexbot.cli import governance_check
from indexbot.core.validate_entry import serialize_package_root
from indexbot.exit_codes import ExitCode
from indexbot.model import Owner, PackageRoot, PullRequestInfo, TagEntry
from tests.fakes import FakeGitHub

_OWNER = Owner(github="alice", github_id=1)
_OTHER_OWNER = Owner(github="bob", github_id=2)
_BASE = "base-sha"
_HEAD = "head-sha"
_ROOT_PATH = "p/kitware/cmake.json"
_STATUS_CONTEXT = "governance/review-required"


def _args(pr_number: int = 1) -> argparse.Namespace:
    return argparse.Namespace(pr_number=pr_number)


def _root(
    *,
    owners: tuple[Owner, ...] = (_OWNER,),
    tags: dict[str, TagEntry] | None = None,
) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/kitware/cmake",
        owners=owners,
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags or {}),
    )


def _github(*, pr_number: int, base: PackageRoot | None, head: PackageRoot) -> FakeGitHub:
    files: dict[tuple[str, str], bytes] = {(_ROOT_PATH, _HEAD): serialize_package_root(head)}
    if base is not None:
        files[(_ROOT_PATH, _BASE)] = serialize_package_root(base)
    info = PullRequestInfo(
        number=pr_number, base_sha=_BASE, head_sha=_HEAD, changed_paths=(_ROOT_PATH,)
    )
    return FakeGitHub(files=files, pull_request_info={pr_number: info})


def test_add_arguments_registers_pr_number() -> None:
    parser = argparse.ArgumentParser()
    governance_check.add_arguments(parser)
    args = parser.parse_args(["--pr-number", "7"])
    assert args.pr_number == 7


def test_refresh_classification_sets_success_status() -> None:
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=before, head=after)

    result = governance_check.run(_args(pr_number=1), github=github)

    assert result == ExitCode.OK
    assert github.statuses[_HEAD] == [
        (_STATUS_CONTEXT, "success", "refresh: no governance review required")
    ]


def test_new_package_classification_sets_pending_status() -> None:
    github = _github(pr_number=1, base=None, head=_root())

    governance_check.run(_args(pr_number=1), github=github)

    context, state, description = github.statuses[_HEAD][0]
    assert context == _STATUS_CONTEXT
    assert state == "pending"
    assert "new-package" in description


def test_human_review_required_classification_sets_pending_status() -> None:
    before = _root(owners=(_OWNER,))
    after = _root(owners=(_OTHER_OWNER,))
    github = _github(pr_number=1, base=before, head=after)

    governance_check.run(_args(pr_number=1), github=github)

    context, state, description = github.statuses[_HEAD][0]
    assert context == _STATUS_CONTEXT
    assert state == "pending"
    assert "human-review-required" in description


def test_run_missing_pull_request_propagates_key_error() -> None:
    github = FakeGitHub()
    with pytest.raises(KeyError):
        governance_check.run(_args(pr_number=99), github=github)
