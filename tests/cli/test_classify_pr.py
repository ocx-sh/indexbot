from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ocx_indexbot.cli import classify_pr
from ocx_indexbot.core.validate_entry import serialize_package_root
from ocx_indexbot.model import Owner, PackageRoot, PullRequestInfo, TagEntry, Yank
from tests.fakes import FakeGitHub, make_policy

_OWNER = Owner(login="alice", id=1)
_OTHER_OWNER = Owner(login="bob", id=2)
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
    labels: tuple[str, ...] = (),
) -> FakeGitHub:
    files: dict[tuple[str, str], bytes] = {}
    for path, root in base_files.items():
        if root is not None:
            files[(path, _BASE)] = serialize_package_root(root)
    for path, root in head_files.items():
        if root is not None:
            files[(path, _HEAD)] = serialize_package_root(root)
    info = PullRequestInfo(
        number=pr_number,
        base_sha=_BASE,
        head_sha=_HEAD,
        changed_paths=changed_paths,
        labels=labels,
    )
    github = FakeGitHub(files=files, pull_request_info={pr_number: info})
    if labels:
        github.labels[pr_number] = list(labels)
    return github


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
    assert classify_pr.classify_pull_request(info, github, policy=make_policy()) == "new-package"


def test_refresh_when_only_tags_change() -> None:
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(
        changed_paths=(_ROOT_PATH,), base_files={_ROOT_PATH: before}, head_files={_ROOT_PATH: after}
    )
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github, policy=make_policy()) == "refresh"


def test_human_review_required_when_owners_change() -> None:
    before = _root(owners=(_OWNER,))
    after = _root(owners=(_OTHER_OWNER,))
    github = _github(
        changed_paths=(_ROOT_PATH,), base_files={_ROOT_PATH: before}, head_files={_ROOT_PATH: after}
    )
    info = github.get_pull_request_info(1)
    assert (
        classify_pr.classify_pull_request(info, github, policy=make_policy())
        == "human-review-required"
    )


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
    assert (
        classify_pr.classify_pull_request(info, github, policy=make_policy())
        == "human-review-required"
    )


def test_deleted_root_is_human_review_required() -> None:
    before = _root()
    github = _github(
        changed_paths=(_ROOT_PATH,), base_files={_ROOT_PATH: before}, head_files={_ROOT_PATH: None}
    )
    info = github.get_pull_request_info(1)
    assert (
        classify_pr.classify_pull_request(info, github, policy=make_policy())
        == "human-review-required"
    )


def test_no_changed_package_roots_is_human_review_required() -> None:
    github = _github(
        changed_paths=(".github/workflows/validate.yml",), base_files={}, head_files={}
    )
    info = github.get_pull_request_info(1)
    assert (
        classify_pr.classify_pull_request(info, github, policy=make_policy())
        == "human-review-required"
    )


def test_cas_object_path_is_excluded_from_root_shape() -> None:
    cas_path = f"p/kitware/cmake/o/sha256/{'a' * 64}.json"
    github = _github(changed_paths=(cas_path,), base_files={}, head_files={})
    info = github.get_pull_request_info(1)
    # No genuine root path in the diff -> conservative default, not a crash
    # trying to parse the CAS object as a root.
    assert (
        classify_pr.classify_pull_request(info, github, policy=make_policy())
        == "human-review-required"
    )


# --- refresh-scope path allowlist (ADR-6 FP-5) ------------------------------


def _refresh_pr(changed_paths: tuple[str, ...]) -> FakeGitHub:
    """A PR whose only root change is refresh-shaped (one tag's content), with
    `changed_paths` under test — everything hinges on which *other* paths ride
    along."""
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    return _github(
        changed_paths=changed_paths,
        base_files={_ROOT_PATH: before},
        head_files={_ROOT_PATH: after},
    )


_OWN_CAS_PATHS = (
    f"p/kitware/cmake/o/sha256/{'b' * 64}.json",
    f"p/kitware/cmake/o/sha256/{'c' * 64}.md",
    f"p/kitware/cmake/o/sha256/{'d' * 64}.svg",
    f"p/kitware/cmake/o/sha256/{'e' * 64}.png",
)


def test_refresh_survives_its_own_packages_cas_objects() -> None:
    github = _refresh_pr((_ROOT_PATH, *_OWN_CAS_PATHS))
    info = github.get_pull_request_info(1)
    assert classify_pr.classify_pull_request(info, github, policy=make_policy()) == "refresh"


_OUT_OF_SCOPE_EXTRAS = [
    ".github/workflows/validate.yml",
    "bot/src/ocx_indexbot/cli/classify_pr.py",
    "README.md",
    # A CAS object under a package whose root is NOT in this diff.
    f"p/acme/widget/o/sha256/{'a' * 64}.json",
    # CAS-shaped but not a CAS object: unknown extension, non-hex digest,
    # traversal in the digest position, and a path past the length cap.
    "p/kitware/cmake/o/sha256/notes.txt",
    f"p/kitware/cmake/o/sha256/{'z' * 64}.json",
    "p/kitware/cmake/o/sha256/../../../../etc/passwd.json",
    f"p/kitware/cmake/o/sha256/{'a' * 300}.json",
]


@pytest.mark.parametrize("extra_path", _OUT_OF_SCOPE_EXTRAS)
def test_out_of_scope_path_forces_human_review(extra_path: str) -> None:
    github = _refresh_pr((_ROOT_PATH, extra_path))
    info = github.get_pull_request_info(1)
    assert (
        classify_pr.classify_pull_request(info, github, policy=make_policy())
        == "human-review-required"
    )


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
    assert classify_pr.classify_pull_request(info, github, policy=make_policy()) == "new-package"


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
    assert (
        classify_pr.classify_pull_request(info, github, policy=make_policy())
        == "human-review-required"
    )


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

    result = classify_pr.run(_args(pr_number=1), github=github, policy=make_policy())

    assert result == classify_pr.ExitCode.OK
    assert github.labels[1] == ["refresh"]
    outputs = output_file.read_text(encoding="utf-8")
    assert "classification" in outputs
    assert "refresh" in outputs


def test_run_missing_pull_request_propagates_key_error() -> None:
    github = FakeGitHub()
    with pytest.raises(KeyError):
        classify_pr.run(_args(pr_number=99), github=github, policy=make_policy())


def test_a_reclassified_pull_request_stops_carrying_its_old_lane_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge request that arrived `human-review-required`, was corrected and
    then merged as `refresh` used to carry both labels for good. `add_labels`
    merges, and nothing ever took the old one off — so the repository's own
    record said automation merged something a human was required to look at.

    No part of the gate is misled by it (every consumer re-derives the class
    from the diff), which is exactly the point: labels are the human's record,
    so a stale one can only mislead a human."""
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(
        changed_paths=(_ROOT_PATH,),
        base_files={_ROOT_PATH: before},
        head_files={_ROOT_PATH: after},
        labels=("human-review-required",),
    )

    assert classify_pr.run(_args(pr_number=1), github=github, policy=make_policy()) == (
        classify_pr.ExitCode.OK
    )

    assert github.labels[1] == ["refresh"]


def test_a_label_a_human_put_there_is_not_swept_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the three lane labels are the classifier's to own. Clearing the
    label set wholesale would delete whatever a maintainer had added, which is
    why this removes named labels rather than assigning the set."""
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(
        changed_paths=(_ROOT_PATH,),
        base_files={_ROOT_PATH: before},
        head_files={_ROOT_PATH: after},
        labels=("needs-registry-access", "human-review-required"),
    )

    classify_pr.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.labels[1] == ["needs-registry-access", "refresh"]


def test_an_unchanged_classification_writes_no_removal() -> None:
    """The common case is a sweep re-confirming the same class every half
    hour. Removing the two it did not pick would be two API writes per open
    merge request per tick, forever, to delete labels that were never there."""
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(
        changed_paths=(_ROOT_PATH,),
        base_files={_ROOT_PATH: before},
        head_files={_ROOT_PATH: after},
        labels=("refresh",),
    )
    info = github.pull_request_info[1]

    classify_pr.apply_change_class(info, "refresh", github)

    assert "remove_label" not in github.calls
