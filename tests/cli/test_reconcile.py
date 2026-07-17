from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from indexbot.cli import reconcile
from indexbot.core.validate_entry import parse_package_root, serialize_package_root
from indexbot.errors import AnomalyError, ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import ManifestFetch, Owner, OwnershipProbeResult, PackageRoot, TagEntry
from tests.fakes import FakeGitHub, FakeRegistry, FixedClock, InMemoryFiles

_OWNER = Owner(github="alice", github_id=1)
_CMAKE_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_WIDGET_REPO = "oci://ghcr.io/ocx-contrib/widget"
_TITLE_KEY = "org.opencontainers.image.title"
_DESCRIPTION_KEY = "org.opencontainers.image.description"
_DESC_TAG = "__ocx.desc"
_BRANCH = "indexbot/reconcile"


def _args(*, dry_run: bool = False, package: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(command="reconcile", dry_run=dry_run, package=package)


def _root(
    name: str = "ocx.sh/kitware/cmake",
    repository: str = _CMAKE_REPO,
    tags: dict[str, TagEntry] | None = None,
) -> PackageRoot:
    return PackageRoot(
        name=name,
        repository=repository,
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags or {}),
    )


def _bare_manifest(architecture: str = "amd64") -> dict[str, object]:
    return {"platform": {"architecture": architecture, "os": "linux"}}


def _put_root(files: InMemoryFiles, namespace: str, package: str, root: PackageRoot) -> None:
    files.write_bytes(f"p/{namespace}/{package}.json", serialize_package_root(root))


def _committed(github: FakeGitHub, path: str) -> bytes:
    return github.files[(path, _BRANCH)]


def _github_output_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))


@dataclass
class _GhostFiles:
    """`FilePort` double: `list_files` reports a root that `read_bytes` can
    no longer find — the list-then-read race `_reconcile_one` tolerates.
    Not `InMemoryFiles` (its single backing dict makes `list_files` and
    `read_bytes` inherently consistent, so this race can't be expressed with
    it alone) — same "standalone double, not a fake" precedent as
    `tests/core/test_observe.py`'s `_RaisingRegistry`.
    """

    listed: list[str] = field(default_factory=list[str])

    def read_text(self, path: str) -> str | None:
        raise AssertionError("not used by this test")

    def write_text(self, path: str, content: str) -> None:
        raise AssertionError("not used by this test")

    def read_bytes(self, path: str) -> bytes | None:
        return None

    def write_bytes(self, path: str, content: bytes) -> None:
        raise AssertionError("not used by this test")

    def exists(self, path: str) -> bool:
        raise AssertionError("not used by this test")

    def list_files(self, prefix: str) -> list[str]:
        return list(self.listed)


class _PoisonRegistry:
    """A `RegistryPort` whose every method raises — proves `_reconcile_one`
    never reaches the network for a committed root whose `repository` isn't
    allowlisted (SSRF ordering, ADR-4 BD-1; mirrors
    `tests/test_validate_entry.py`'s `_PoisonRegistry`)."""

    def list_tags(self, repository: str) -> list[str]:
        raise AssertionError("registry.list_tags must never be called (SSRF ordering)")

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        raise AssertionError("registry.get_manifest must never be called (SSRF ordering)")

    def get_desc_tag_digest(self, repository: str) -> str | None:
        raise AssertionError("registry.get_desc_tag_digest must never be called (SSRF ordering)")

    def get_blob(self, repository: str, digest: str) -> bytes:
        raise AssertionError("registry.get_blob must never be called (SSRF ordering)")

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        raise AssertionError("registry.probe_ownership must never be called (SSRF ordering)")


def test_unallowlisted_repository_raises_before_any_registry_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(repository="oci://evil.example.com/x/y"))

    with pytest.raises(ValidationError, match="G-03"):
        reconcile.run(
            _args(),
            files=files,
            registry=_PoisonRegistry(),
            github=FakeGitHub(),
            clock=FixedClock(),
        )


def test_empty_index_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _github_output_env(tmp_path, monkeypatch)
    github = FakeGitHub()

    result = reconcile.run(
        _args(), files=InMemoryFiles(), registry=FakeRegistry(), github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK
    assert github.pull_requests == {}
    assert github.refs == {}


def test_missing_args_attributes_default_to_full_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `Namespace` without `dry_run`/`package` still works —
    `getattr(..., default)` covers a caller that hasn't wired those argparse
    flags yet."""
    _github_output_env(tmp_path, monkeypatch)
    bare_args = argparse.Namespace(command="reconcile")

    result = reconcile.run(
        bare_args,
        files=InMemoryFiles(),
        registry=FakeRegistry(),
        github=FakeGitHub(),
        clock=FixedClock(),
    )

    assert result == ExitCode.OK


def test_clean_drift_opens_pr_and_excludes_cas_subtree_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(tags={}))
    # A CAS object path under the same package — three segments under `p/`,
    # must never be mistaken for a second root.
    files.write_bytes(f"p/kitware/cmake/o/sha256/{'a' * 64}.json", b"{}")

    registry = FakeRegistry(
        tags={_CMAKE_REPO: ["3.28.1"]}, manifests={(_CMAKE_REPO, "3.28.1"): _bare_manifest()}
    )
    github = FakeGitHub(refs={"main": "sha-main-0"})

    result = reconcile.run(
        _args(), files=files, registry=registry, github=github, clock=FixedClock(fixed="T1")
    )

    assert result == ExitCode.OK
    assert github.pull_requests == {_BRANCH: 1}
    committed_root = parse_package_root(_committed(github, "p/kitware/cmake.json"))
    assert committed_root.tags["3.28.1"].observed == "T1"
    cas_paths = [
        path for path, branch in github.files if branch == _BRANCH and "/o/sha256/" in path
    ]
    assert len(cas_paths) == 1  # exactly one new CAS object, not the pre-seeded decoy


def test_dry_run_reports_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(tags={}))
    registry = FakeRegistry(
        tags={_CMAKE_REPO: ["3.28.1"]}, manifests={(_CMAKE_REPO, "3.28.1"): _bare_manifest()}
    )
    github = FakeGitHub(refs={"main": "sha-main-0"})

    result = reconcile.run(
        _args(dry_run=True), files=files, registry=registry, github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK
    assert github.pull_requests == {}
    assert github.refs == {"main": "sha-main-0"}  # unchanged — no branch created


def test_second_run_with_no_registry_change_is_idempotent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(tags={}))
    registry = FakeRegistry(
        tags={_CMAKE_REPO: ["3.28.1"]}, manifests={(_CMAKE_REPO, "3.28.1"): _bare_manifest()}
    )
    github = FakeGitHub(refs={"main": "sha-main-0"})

    reconcile.run(
        _args(), files=files, registry=registry, github=github, clock=FixedClock(fixed="T1")
    )
    # Feed the just-committed root back in as the new "current" source tree —
    # a real second `reconcile` run would read exactly this state.
    _put_root(
        files, "kitware", "cmake", parse_package_root(_committed(github, "p/kitware/cmake.json"))
    )

    result = reconcile.run(
        _args(), files=files, registry=registry, github=github, clock=FixedClock(fixed="T2")
    )

    assert result == ExitCode.OK
    assert github.pull_requests == {_BRANCH: 1}  # no second PR opened — nothing changed


def test_package_scope_filters_to_one_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(tags={}))
    _put_root(
        files, "acme", "widget", _root(name="ocx.sh/acme/widget", repository=_WIDGET_REPO, tags={})
    )
    registry = FakeRegistry(
        tags={_CMAKE_REPO: ["3.28.1"], _WIDGET_REPO: ["1.0.0"]},
        manifests={
            (_CMAKE_REPO, "3.28.1"): _bare_manifest(),
            (_WIDGET_REPO, "1.0.0"): _bare_manifest(),
        },
    )
    github = FakeGitHub(refs={"main": "sha-main-0"})

    reconcile.run(
        _args(package="kitware/cmake"),
        files=files,
        registry=registry,
        github=github,
        clock=FixedClock(),
    )

    assert ("p/kitware/cmake.json", _BRANCH) in github.files
    assert ("p/acme/widget.json", _BRANCH) not in github.files


def test_ghost_root_vanished_between_list_and_read_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = _GhostFiles(listed=["p/kitware/cmake.json"])

    result = reconcile.run(
        _args(), files=files, registry=FakeRegistry(), github=FakeGitHub(), clock=FixedClock()
    )

    assert result == ExitCode.OK


def test_anomaly_raises_after_committing_clean_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(tags={}))  # clean: gains a new tag
    stale_digest = "sha256:" + "0" * 64
    _put_root(
        files,
        "acme",
        "widget",
        _root(
            name="ocx.sh/acme/widget",
            repository=_WIDGET_REPO,
            tags={"1.0.0": TagEntry(content=stale_digest, observed="T0")},
        ),
    )
    registry = FakeRegistry(
        tags={_CMAKE_REPO: ["3.28.1"], _WIDGET_REPO: ["1.0.0"]},
        manifests={
            (_CMAKE_REPO, "3.28.1"): _bare_manifest(),
            (_WIDGET_REPO, "1.0.0"): _bare_manifest(architecture="arm64"),
        },
    )
    github = FakeGitHub(refs={"main": "sha-main-0"})

    with pytest.raises(AnomalyError, match="1 anomaly") as exc_info:
        reconcile.run(_args(), files=files, registry=registry, github=github, clock=FixedClock())

    # Partial-success: the clean package's PR was still opened before raising.
    assert ("p/kitware/cmake.json", _BRANCH) in github.files
    assert ("p/acme/widget.json", _BRANCH) not in github.files
    assert "acme/widget 1.0.0" in str(exc_info.value)


def test_base_branch_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(tags={}))
    registry = FakeRegistry(
        tags={_CMAKE_REPO: ["3.28.1"]}, manifests={(_CMAKE_REPO, "3.28.1"): _bare_manifest()}
    )
    github = FakeGitHub()  # no "main" ref configured at all

    with pytest.raises(RuntimeError, match="does not exist"):
        reconcile.run(_args(), files=files, registry=registry, github=github, clock=FixedClock())


def test_reuses_already_open_reconcile_branch_as_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(tags={}))
    registry = FakeRegistry(
        tags={_CMAKE_REPO: ["3.28.1"]}, manifests={(_CMAKE_REPO, "3.28.1"): _bare_manifest()}
    )
    github = FakeGitHub(refs={"main": "sha-main-0", _BRANCH: "sha-prev-open-pr"})
    github.pull_requests[_BRANCH] = 7  # a PR from a previous nightly run is still open

    result = reconcile.run(
        _args(), files=files, registry=registry, github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK
    assert github.pull_requests[_BRANCH] == 7  # same PR reused, not a second one


def test_desc_change_readme_only_no_logo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(tags={}))
    readme_bytes = b"# CMake\n"
    registry = FakeRegistry(
        tags={_CMAKE_REPO: []},
        desc_digests={_CMAKE_REPO: "sha256:" + "b" * 64},
        manifests={
            (_CMAKE_REPO, _DESC_TAG): {
                "annotations": {_TITLE_KEY: "CMake", _DESCRIPTION_KEY: "Build tool"},
                "layers": [{"mediaType": "application/markdown", "digest": "sha256:" + "c" * 64}],
            }
        },
        blobs={(_CMAKE_REPO, "sha256:" + "c" * 64): readme_bytes},
    )
    github = FakeGitHub(refs={"main": "sha-main-0"})

    result = reconcile.run(
        _args(), files=files, registry=registry, github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK
    readme_paths = [
        path for path, branch in github.files if branch == _BRANCH and path.endswith(".md")
    ]
    assert len(readme_paths) == 1
    assert _committed(github, readme_paths[0]) == readme_bytes


def test_desc_change_with_png_and_svg_logos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_output_env(tmp_path, monkeypatch)
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(tags={}))
    _put_root(
        files, "acme", "widget", _root(name="ocx.sh/acme/widget", repository=_WIDGET_REPO, tags={})
    )

    png_bytes = b"\x89PNG\r\n\x1a\nrest-of-file"
    svg_bytes = b"<svg></svg>"
    registry = FakeRegistry(
        tags={_CMAKE_REPO: [], _WIDGET_REPO: []},
        desc_digests={_CMAKE_REPO: "sha256:" + "1" * 64, _WIDGET_REPO: "sha256:" + "2" * 64},
        manifests={
            (_CMAKE_REPO, _DESC_TAG): {
                "annotations": {_TITLE_KEY: "CMake", _DESCRIPTION_KEY: "d"},
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:" + "3" * 64},
                    {"mediaType": "image/png", "digest": "sha256:" + "4" * 64},
                ],
            },
            (_WIDGET_REPO, _DESC_TAG): {
                "annotations": {_TITLE_KEY: "Widget", _DESCRIPTION_KEY: "d"},
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:" + "5" * 64},
                    {"mediaType": "image/svg+xml", "digest": "sha256:" + "6" * 64},
                ],
            },
        },
        blobs={
            (_CMAKE_REPO, "sha256:" + "3" * 64): b"# CMake\n",
            (_CMAKE_REPO, "sha256:" + "4" * 64): png_bytes,
            (_WIDGET_REPO, "sha256:" + "5" * 64): b"# Widget\n",
            (_WIDGET_REPO, "sha256:" + "6" * 64): svg_bytes,
        },
    )
    github = FakeGitHub(refs={"main": "sha-main-0"})

    reconcile.run(_args(), files=files, registry=registry, github=github, clock=FixedClock())

    # CAS paths are keyed by a freshly computed content digest (of the raw
    # readme/logo bytes), not by the registry blob digests used above to
    # fetch them — assert on the resulting file *extensions* instead.
    cas_exts = {
        path.rsplit(".", 1)[-1]
        for path, branch in github.files
        if branch == _BRANCH and "/o/sha256/" in path
    }
    assert "png" in cas_exts
    assert "svg" in cas_exts
