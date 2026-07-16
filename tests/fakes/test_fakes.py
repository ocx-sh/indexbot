"""Basic behavior tests for the in-memory Protocol fakes.

Not required by the coverage gate (fakes live under `tests/`), but Phase 2's
`core/` suites need to trust these — so each fake gets a happy-path and a
miss/idempotency case.
"""

from __future__ import annotations

import pytest

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
    assert registry.get_manifest("repo", "3.28.1") == manifest


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


def test_fixed_clock_default() -> None:
    clock = FixedClock()
    assert clock.now_iso8601() == "2026-07-17T00:00:00Z"


def test_fixed_clock_custom_instant() -> None:
    clock = FixedClock(fixed="2026-01-01T00:00:00Z")
    assert clock.now_iso8601() == "2026-01-01T00:00:00Z"
