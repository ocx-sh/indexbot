from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from indexbot.cli import classify_pr
from indexbot.core.validate_entry import serialize_package_root
from indexbot.model import Owner, PackageRoot, PullRequestInfo, TagEntry, Yank
from tests.fakes import FakeGitHub

_OWNER = Owner(github="alice", github_id=1)
_OTHER_OWNER = Owner(github="bob", github_id=2)
_BASE = "base-sha"
_HEAD = "head-sha"
_ROOT_PATH = "p/kitware/cmake.json"
_OTHER_ROOT_PATH = "p/acme/widget.json"


def _args(pr_number: int = 1) -> argparse.Namespace:
    return argparse.Namespace(pr_number=pr_number)


def _root(
    name: str = "ocx.sh/kitware/cmake",
    *,
    owners: tuple[Owner, ...] = (_OWNER,),
    repository: str = "oci://ghcr.io/kitware/cmake",
    status: str = "active",
    deprecated_message: str | None = None,
    tags: dict[str, TagEntry] | None = None,
) -> PackageRoot:
    return PackageRoot(
        name=name,
        repository=repository,
        owners=owners,
        status=status,  # type: ignore[arg-type]
        deprecated_message=deprecated_message,
        created="2026-07-17",
        desc=None,
        tags=dict(tags or {}),
    )


def _github(
    *,
    pr_number: int = 1,
    changed_paths: tuple[str, ...],
    base_files: dict[str, PackageRoot | None],
    head_files: dict[str, PackageRoot | None],
) -> FakeGitHub:
    files: dict[tuple[str, str], bytes] = {}
    for path, root in base_files.items():
        if root is not None:
            files[(path, _BASE)] = serialize_package_root(root)
    for path, root in head_files.items():
        if root is not None:
            files[(path, _HEAD)] = serialize_package_root(root)
    info = PullRequestInfo(
        number=pr_number, base_sha=_BASE, head_sha=_HEAD, changed_paths=changed_paths
    )
    return FakeGitHub(files=files, pull_request_info={pr_number: info})


# --- add_arguments ---------------------------------------------------------


def test_add_arguments_registers_pr_number() -> None:
    parser = argparse.ArgumentParser()
    classify_pr.add_arguments(parser)
    args = parser.parse_args(["--pr-number", "42"])
    assert args.pr_number == 42


# --- classify_pull_request: per-branch classification -----------------------


def test_new_package_root_added_with_no_base_file() -> None:
    github = _github(
        changed_paths=(_ROOT_PATH,),
        base_files={_ROOT_PATH: None},
        head_files={_ROOT_PATH: _root()},
    )
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github) == "new-package"


def test_refresh_when_only_tags_change() -> None:
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(
        changed_paths=(_ROOT_PATH,), base_files={_ROOT_PATH: before}, head_files={_ROOT_PATH: after}
    )
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github) == "refresh"


def test_human_review_required_when_owners_change() -> None:
    before = _root(owners=(_OWNER,))
    after = _root(owners=(_OTHER_OWNER,))
    github = _github(
        changed_paths=(_ROOT_PATH,), base_files={_ROOT_PATH: before}, head_files={_ROOT_PATH: after}
    )
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github) == "human-review-required"


def test_human_review_required_when_a_tag_is_yanked() -> None:
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(
        tags={
            "1.0.0": TagEntry(
                content="sha256:" + "a" * 64,
                observed="T0",
                yanked=Yank(reason="cve", at="T1"),
            )
        }
    )
    github = _github(
        changed_paths=(_ROOT_PATH,), base_files={_ROOT_PATH: before}, head_files={_ROOT_PATH: after}
    )
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github) == "human-review-required"


def test_deleted_root_is_human_review_required() -> None:
    before = _root()
    github = _github(
        changed_paths=(_ROOT_PATH,), base_files={_ROOT_PATH: before}, head_files={_ROOT_PATH: None}
    )
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github) == "human-review-required"


def test_no_changed_package_roots_is_human_review_required() -> None:
    github = _github(
        changed_paths=(".github/workflows/validate.yml",), base_files={}, head_files={}
    )
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github) == "human-review-required"


def test_cas_object_path_is_excluded_from_root_shape() -> None:
    cas_path = f"p/kitware/cmake/o/sha256/{'a' * 64}.json"
    github = _github(changed_paths=(cas_path,), base_files={}, head_files={})
    info = github.get_pull_request_info(1)
    # No genuine root path in the diff -> conservative default, not a crash
    # trying to parse the CAS object as a root.
    assert classify_pr.classify_pull_request(info, github) == "human-review-required"


# --- worst-classification-wins aggregation ----------------------------------


def test_worst_wins_refresh_and_new_package_yields_new_package() -> None:
    refresh_before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    refresh_after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(
        changed_paths=(_ROOT_PATH, _OTHER_ROOT_PATH),
        base_files={_ROOT_PATH: refresh_before, _OTHER_ROOT_PATH: None},
        head_files={
            _ROOT_PATH: refresh_after,
            _OTHER_ROOT_PATH: _root(name="ocx.sh/acme/widget"),
        },
    )
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github) == "new-package"


def test_worst_wins_new_package_and_human_review_yields_human_review() -> None:
    review_before = _root(owners=(_OWNER,))
    review_after = _root(owners=(_OTHER_OWNER,))
    github = _github(
        changed_paths=(_ROOT_PATH, _OTHER_ROOT_PATH),
        base_files={_ROOT_PATH: None, _OTHER_ROOT_PATH: review_before},
        head_files={
            _ROOT_PATH: _root(name="ocx.sh/kitware/cmake"),
            _OTHER_ROOT_PATH: review_after,
        },
    )
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github) == "human-review-required"


# --- run() -------------------------------------------------------------------


def test_run_applies_label_and_writes_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(
        changed_paths=(_ROOT_PATH,), base_files={_ROOT_PATH: before}, head_files={_ROOT_PATH: after}
    )

    result = classify_pr.run(_args(pr_number=1), github=github)

    assert result == classify_pr.ExitCode.OK
    assert github.labels[1] == ["refresh"]
    outputs = output_file.read_text(encoding="utf-8")
    assert "classification" in outputs
    assert "refresh" in outputs


def test_run_missing_pull_request_propagates_key_error() -> None:
    github = FakeGitHub()
    with pytest.raises(KeyError):
        classify_pr.run(_args(pr_number=99), github=github)
