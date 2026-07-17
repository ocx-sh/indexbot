"""Basic behavior tests for the in-memory Protocol fakes.

Not required by the coverage gate (fakes live under `tests/`), but Phase 2's
`core/` suites need to trust these — so each fake gets a happy-path and a
miss/idempotency case.
"""

from __future__ import annotations

import hashlib

import pytest

from indexbot.errors import TransientError, ValidationError
from indexbot.model import PullRequestInfo

from . import FakeGitHub, FakeRegistry, FixedClock, InMemoryFiles


def test_fake_registry_list_tags_empty_for_unknown_repository() -> None:
    registry = FakeRegistry()
    assert registry.list_tags("ghcr.io/ocx-contrib/cmake") == []


def test_fake_registry_list_tags_returns_configured_tags() -> None:
    registry = FakeRegistry(tags={"ghcr.io/ocx-contrib/cmake": ["3.28.1", "latest"]})
    assert registry.list_tags("ghcr.io/ocx-contrib/cmake") == ["3.28.1", "latest"]


def test_fake_registry_get_manifest_found() -> None:
    manifest: dict[str, object] = {"platforms": []}
    registry = FakeRegistry(manifests={("repo", "3.28.1"): manifest})
    fetch = registry.get_manifest("repo", "3.28.1")
    assert fetch.parsed == manifest


def test_fake_registry_get_manifest_digest_is_computed_from_content() -> None:
    # Same input twice -> byte-identical `raw` -> the same digest; never a
    # value the test configures directly (mirrors `adapters/ghcr.py`'s
    # digest doctrine, `ports.py`'s `get_manifest` docstring).
    manifest: dict[str, object] = {"platforms": []}
    first = FakeRegistry(manifests={("repo", "a"): manifest}).get_manifest("repo", "a")
    second = FakeRegistry(manifests={("repo", "b"): manifest}).get_manifest("repo", "b")
    assert first.digest == second.digest
    assert first.digest.startswith("sha256:")
    assert hashlib.sha256(first.raw).hexdigest() == first.digest.removeprefix("sha256:")


def test_fake_registry_get_manifest_missing_raises_key_error() -> None:
    registry = FakeRegistry()
    with pytest.raises(KeyError):
        registry.get_manifest("repo", "missing")


def test_fake_registry_desc_digest_absent_by_default() -> None:
    registry = FakeRegistry()
    assert registry.get_desc_tag_digest("repo") is None


def test_fake_registry_desc_digest_present() -> None:
    registry = FakeRegistry(desc_digests={"repo": "sha256:aaaa"})
    assert registry.get_desc_tag_digest("repo") == "sha256:aaaa"


def test_fake_registry_get_blob_found() -> None:
    registry = FakeRegistry(blobs={("repo", "sha256:aaaa"): b"# CMake\n"})
    assert registry.get_blob("repo", "sha256:aaaa") == b"# CMake\n"


def test_fake_registry_get_blob_missing_raises_key_error() -> None:
    registry = FakeRegistry()
    with pytest.raises(KeyError):
        registry.get_blob("repo", "sha256:missing")


def test_fake_registry_probe_ownership_defaults_unconfirmed() -> None:
    registry = FakeRegistry()
    assert registry.probe_ownership("repo", "ocx.sh/kitware/cmake") == "unconfirmed"


def test_fake_registry_probe_ownership_configured() -> None:
    registry = FakeRegistry(ownership={"repo": "confirmed"})
    assert registry.probe_ownership("repo", "ocx.sh/kitware/cmake") == "confirmed"


def test_fake_github_get_file_contents_missing_returns_none() -> None:
    github = FakeGitHub()
    assert github.get_file_contents("p/kitware/cmake.json", "main") is None


def test_fake_github_get_file_contents_present() -> None:
    github = FakeGitHub(files={("p/kitware/cmake.json", "main"): b"{}"})
    assert github.get_file_contents("p/kitware/cmake.json", "main") == b"{}"


def test_fake_github_open_pull_request_assigns_incrementing_numbers() -> None:
    github = FakeGitHub()
    first = github.open_or_update_pull_request(
        branch="announce/kitware-cmake", base="main", title="t", body="b"
    )
    second = github.open_or_update_pull_request(
        branch="announce/astral-uv", base="main", title="t2", body="b2"
    )
    assert first == 1
    assert second == 2


def test_fake_github_open_pull_request_is_idempotent_per_branch() -> None:
    github = FakeGitHub()
    first = github.open_or_update_pull_request(
        branch="announce/kitware-cmake", base="main", title="t", body="b"
    )
    again = github.open_or_update_pull_request(
        branch="announce/kitware-cmake", base="main", title="t (updated)", body="b (updated)"
    )
    assert first == again


def test_fake_github_get_ref_sha_missing_returns_none() -> None:
    github = FakeGitHub()
    assert github.get_ref_sha("announce/kitware-cmake") is None


def test_fake_github_commit_files_creates_branch_and_writes_files() -> None:
    github = FakeGitHub()
    sha = github.commit_files(
        branch="announce/kitware-cmake",
        base_sha="main-sha",
        message="regenerate kitware/cmake",
        files={"p/kitware/cmake.json": b"{}"},
    )
    assert github.get_ref_sha("announce/kitware-cmake") == sha
    assert github.get_file_contents("p/kitware/cmake.json", "announce/kitware-cmake") == b"{}"


def test_fake_github_commit_files_fast_forwards_matching_base_sha() -> None:
    github = FakeGitHub()
    first = github.commit_files(
        branch="announce/kitware-cmake",
        base_sha="main-sha",
        message="first",
        files={"p/kitware/cmake.json": b"{}"},
    )
    second = github.commit_files(
        branch="announce/kitware-cmake",
        base_sha=first,
        message="second",
        files={"p/kitware/cmake.json": b'{"tags": {}}'},
    )
    assert second != first
    updated = github.get_file_contents("p/kitware/cmake.json", "announce/kitware-cmake")
    assert updated == b'{"tags": {}}'


def test_fake_github_commit_files_stale_base_sha_raises_transient_error() -> None:
    github = FakeGitHub()
    github.commit_files(
        branch="announce/kitware-cmake",
        base_sha="main-sha",
        message="first",
        files={"p/kitware/cmake.json": b"{}"},
    )
    with pytest.raises(TransientError):
        github.commit_files(
            branch="announce/kitware-cmake",
            base_sha="main-sha",  # stale — branch has already moved
            message="second",
            files={"p/kitware/cmake.json": b"{}"},
        )


def test_fake_github_commit_files_none_content_deletes_path() -> None:
    github = FakeGitHub()
    github.commit_files(
        branch="reconcile/main",
        base_sha="main-sha",
        message="first",
        files={"p/kitware/cmake.json": b"{}"},
    )
    github.commit_files(
        branch="reconcile/main",
        base_sha=github.get_ref_sha("reconcile/main"),  # type: ignore[arg-type]
        message="second",
        files={"p/kitware/cmake.json": None},
    )
    assert github.get_file_contents("p/kitware/cmake.json", "reconcile/main") is None


def test_fake_github_get_pull_request_info_missing_raises_key_error() -> None:
    github = FakeGitHub()
    with pytest.raises(KeyError):
        github.get_pull_request_info(1)


def test_fake_github_get_pull_request_info_returns_configured_value() -> None:
    info = PullRequestInfo(
        number=1, base_sha="aaa", head_sha="bbb", changed_paths=("p/kitware/cmake.json",)
    )
    github = FakeGitHub(pull_request_info={1: info})
    assert github.get_pull_request_info(1) is info


def test_fake_github_set_commit_status_accumulates() -> None:
    github = FakeGitHub()
    github.set_commit_status(
        "bbb", context="governance/review-required", state="pending", description="classifying"
    )
    github.set_commit_status(
        "bbb", context="governance/review-required", state="success", description="refresh, clean"
    )
    assert github.statuses["bbb"] == [
        ("governance/review-required", "pending", "classifying"),
        ("governance/review-required", "success", "refresh, clean"),
    ]


def test_fake_github_add_labels_accumulates() -> None:
    github = FakeGitHub()
    pr = github.open_or_update_pull_request(branch="b", base="main", title="t", body="b")
    github.add_labels(pr, ["new-package"])
    github.add_labels(pr, ["needs-review"])
    assert github.labels[pr] == ["new-package", "needs-review"]


def test_fake_github_enable_auto_merge() -> None:
    github = FakeGitHub()
    pr = github.open_or_update_pull_request(branch="b", base="main", title="t", body="b")
    assert pr not in github.auto_merge_enabled
    github.enable_auto_merge(pr)
    assert pr in github.auto_merge_enabled


def test_in_memory_files_round_trip() -> None:
    files = InMemoryFiles()
    assert files.exists("p/kitware/cmake.json") is False
    assert files.read_text("p/kitware/cmake.json") is None
    files.write_text("p/kitware/cmake.json", "{}")
    assert files.exists("p/kitware/cmake.json") is True
    assert files.read_text("p/kitware/cmake.json") == "{}"


def test_in_memory_files_bytes_round_trip() -> None:
    files = InMemoryFiles()
    assert files.read_bytes("p/kitware/cmake/o/sha256/aaaa.svg") is None
    files.write_bytes("p/kitware/cmake/o/sha256/aaaa.svg", b"<svg/>")
    assert files.read_bytes("p/kitware/cmake/o/sha256/aaaa.svg") == b"<svg/>"


def test_in_memory_files_text_and_bytes_share_one_store() -> None:
    files = InMemoryFiles()
    files.write_text("p/kitware/cmake.json", "{}")
    assert files.read_bytes("p/kitware/cmake.json") == b"{}"


def test_in_memory_files_list_files_empty_for_unknown_prefix() -> None:
    files = InMemoryFiles()
    assert files.list_files("p/kitware") == []


def test_in_memory_files_list_files_returns_sorted_matches() -> None:
    files = InMemoryFiles()
    files.write_text("p/kitware/cmake.json", "{}")
    files.write_text("p/astral-sh/uv.json", "{}")
    assert files.list_files("p") == ["p/astral-sh/uv.json", "p/kitware/cmake.json"]


def test_in_memory_files_list_files_respects_directory_boundary() -> None:
    # "p/kitware" must not match "p/kitware-fork/x.json" — prefix matching is
    # directory-scoped, not a raw string prefix.
    files = InMemoryFiles()
    files.write_text("p/kitware/cmake.json", "{}")
    files.write_text("p/kitware-fork/x.json", "{}")
    assert files.list_files("p/kitware") == ["p/kitware/cmake.json"]


# --- traversal matrix — mirrors tests/adapters/test_local_files.py --------


def test_in_memory_files_dotdot_traversal_rejected_on_read() -> None:
    files = InMemoryFiles()
    with pytest.raises(ValidationError, match="escapes root"):
        files.read_text("../outside.txt")


def test_in_memory_files_dotdot_traversal_rejected_before_write() -> None:
    files = InMemoryFiles()
    with pytest.raises(ValidationError, match="escapes root"):
        files.write_text("../escape.txt", "pwned")
    assert files.files == {}


def test_in_memory_files_absolute_path_rejected() -> None:
    files = InMemoryFiles()
    with pytest.raises(ValidationError, match="escapes root"):
        files.read_text("/etc/passwd")


def test_in_memory_files_absolute_path_rejected_before_write() -> None:
    files = InMemoryFiles()
    with pytest.raises(ValidationError, match="escapes root"):
        files.write_bytes("/etc/passwd", b"pwned")
    assert files.files == {}


def test_in_memory_files_exists_rejects_traversal() -> None:
    files = InMemoryFiles()
    with pytest.raises(ValidationError, match="escapes root"):
        files.exists("../outside.txt")


def test_in_memory_files_read_bytes_rejects_traversal() -> None:
    files = InMemoryFiles()
    with pytest.raises(ValidationError, match="escapes root"):
        files.read_bytes("../outside.svg")


def test_in_memory_files_list_files_rejects_traversal_prefix() -> None:
    files = InMemoryFiles()
    with pytest.raises(ValidationError, match="escapes root"):
        files.list_files("..")


def test_in_memory_files_internal_dotdot_within_root_is_allowed() -> None:
    # Mirrors a real filesystem's `Path.resolve()`: `..` that stays inside
    # the root is fine, only escaping it is rejected — matches
    # `adapters/local_files.py`'s equivalent behavior.
    files = InMemoryFiles()
    files.write_text("p/kitware/cmake.json", "{}")
    assert files.read_text("p/kitware/../kitware/cmake.json") == "{}"


def test_fixed_clock_default() -> None:
    clock = FixedClock()
    assert clock.now_iso8601() == "2026-07-17T00:00:00Z"


def test_fixed_clock_custom_instant() -> None:
    clock = FixedClock(fixed="2026-01-01T00:00:00Z")
    assert clock.now_iso8601() == "2026-01-01T00:00:00Z"
