from __future__ import annotations

import argparse
from dataclasses import dataclass

import pytest

from indexbot.cli import announce
from indexbot.core.observe import observe_one_tag
from indexbot.core.validate_entry import parse_package_root, serialize_package_root
from indexbot.errors import TransientError, ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import ManifestFetch, Owner, OwnershipProbeResult, PackageRoot, TagEntry, Yank
from tests.fakes import FakeGitHub, FakeRegistry, FixedClock, InMemoryFiles

_NS = "kitware"
_PKG = "cmake"
_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_ROOT_PATH = f"p/{_NS}/{_PKG}.json"
_BRANCH = f"indexbot-announce-{_NS}-{_PKG}"
_OWNER = Owner(github="alice", github_id=1)


@dataclass
class _RaisingRegistry:
    """Minimal standalone `RegistryPort` double proving a code path never
    reaches the network — not `FakeRegistry` (consume-only, never edited)."""

    def list_tags(self, repository: str) -> list[str]:
        raise TransientError("registry backoff exhausted (test double)")

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        raise AssertionError("should not be called")

    def get_desc_tag_digest(self, repository: str) -> str | None:
        raise AssertionError("should not be called")

    def get_blob(self, repository: str, digest: str) -> bytes:
        raise AssertionError("should not be called")

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        raise AssertionError("should not be called")


def _args(
    *,
    package: str = f"{_NS}/{_PKG}",
    tags: str | None = "3.28.1",
    tags_file: str | None = None,
    out: str | None = "out",
    fork: str | None = None,
    index_repo: str = "ocx-sh/index",
    yank: list[str] | None = None,
    unyank: list[str] | None = None,
    yank_reason: str = "yanked via announce",
) -> argparse.Namespace:
    return argparse.Namespace(
        package=package,
        tags=tags,
        tags_file=tags_file,
        out=out,
        fork=fork,
        index_repo=index_repo,
        yank=yank or [],
        unyank=unyank or [],
        yank_reason=yank_reason,
    )


def _root(tags: dict[str, TagEntry], *, repository: str = _REPO) -> PackageRoot:
    return PackageRoot(
        name=f"ocx.sh/{_NS}/{_PKG}",
        repository=repository,
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags),
    )


def _manifest(digest: str) -> dict[str, object]:
    return {"platform": {"architecture": "amd64", "os": "linux"}, "digest": digest}


def _observed_content_digest(tag: str, manifest_digest: str) -> str:
    registry = FakeRegistry(
        tags={_REPO: [tag]}, manifests={(_REPO, tag): _manifest(manifest_digest)}
    )
    observation = observe_one_tag(_REPO, tag, registry)
    assert observation is not None
    return observation.content_digest


# --- add_arguments -----------------------------------------------------


def test_add_arguments_requires_package() -> None:
    parser = argparse.ArgumentParser()
    announce.add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--tags", "1.0.0", "--out", "dist"])


def test_add_arguments_tags_and_tags_file_are_mutually_exclusive() -> None:
    parser = argparse.ArgumentParser()
    announce.add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--package", "ns/pkg", "--tags", "1.0.0", "--tags-file", "tags.txt", "--out", "dist"]
        )


def test_add_arguments_out_and_fork_are_mutually_exclusive() -> None:
    parser = argparse.ArgumentParser()
    announce.add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--package", "ns/pkg", "--tags", "1.0.0", "--out", "dist", "--fork", "alice/index"]
        )


def test_add_arguments_index_repo_defaults() -> None:
    parser = argparse.ArgumentParser()
    announce.add_arguments(parser)
    parsed = parser.parse_args(["--package", "ns/pkg", "--tags", "1.0.0", "--out", "dist"])
    assert parsed.index_repo == "ocx-sh/index"


def test_add_arguments_parses_yank_and_unyank_lists() -> None:
    parser = argparse.ArgumentParser()
    announce.add_arguments(parser)
    parsed = parser.parse_args(
        [
            "--package",
            "ns/pkg",
            "--tags",
            "1.0.0",
            "--out",
            "dist",
            "--yank",
            "0.9.0",
            "--unyank",
            "0.8.0",
        ]
    )
    assert parsed.yank == ["0.9.0"]
    assert parsed.unyank == ["0.8.0"]


# --- unclaimed namespace / SSRF ordering / typo tags ------------------------


def test_unclaimed_namespace_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="unclaimed"):
        announce.run(
            _args(),
            registry=_RaisingRegistry(),
            index_github=FakeGitHub(),
            fork_github=None,
            files=InMemoryFiles(),
            clock=FixedClock(),
        )


def test_repository_allowlist_checked_before_any_registry_call() -> None:
    current = _root({}, repository="oci://evil.example.com/ns/pkg")
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})

    with pytest.raises(ValidationError, match="allowlisted"):
        announce.run(
            _args(),
            registry=_RaisingRegistry(),
            index_github=index_github,
            fork_github=None,
            files=InMemoryFiles(),
            clock=FixedClock(),
        )


def test_curated_tag_typo_raises_validation_error() -> None:
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    registry = FakeRegistry()  # no tags/manifests registered at all

    with pytest.raises(ValidationError, match="does not resolve"):
        announce.run(
            _args(tags="9.9.9-typo"),
            registry=registry,
            index_github=index_github,
            fork_github=None,
            files=InMemoryFiles(),
            clock=FixedClock(),
        )


def test_empty_tags_raises_validation_error() -> None:
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})

    with pytest.raises(ValidationError, match="no tags given"):
        announce.run(
            _args(tags="   ,  "),
            registry=FakeRegistry(),
            index_github=index_github,
            fork_github=None,
            files=InMemoryFiles(),
            clock=FixedClock(),
        )


def test_tags_file_missing_raises_validation_error() -> None:
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})

    with pytest.raises(ValidationError, match="does not exist"):
        announce.run(
            _args(tags=None, tags_file="missing.txt"),
            registry=FakeRegistry(),
            index_github=index_github,
            fork_github=None,
            files=InMemoryFiles(),
            clock=FixedClock(),
        )


# --- --tags-file resolution --------------------------------------------


def test_tags_file_comma_separated() -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    registry = FakeRegistry(
        tags={_REPO: ["1.0.0"]}, manifests={(_REPO, "1.0.0"): _manifest(manifest_digest)}
    )
    files = InMemoryFiles(files={"tags.txt": b"1.0.0"})

    result = announce.run(
        _args(tags=None, tags_file="tags.txt", out="dist"),
        registry=registry,
        index_github=index_github,
        fork_github=None,
        files=files,
        clock=FixedClock(),
    )

    assert result == ExitCode.OK
    committed = parse_package_root(files.read_bytes(f"dist/{_ROOT_PATH}"))  # type: ignore[arg-type]
    assert "1.0.0" in committed.tags


def test_tags_file_newline_separated() -> None:
    manifest_digest_a = "sha256:" + "1" * 64
    manifest_digest_b = "sha256:" + "2" * 64
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    registry = FakeRegistry(
        tags={_REPO: ["1.0.0", "2.0.0"]},
        manifests={
            (_REPO, "1.0.0"): _manifest(manifest_digest_a),
            (_REPO, "2.0.0"): _manifest(manifest_digest_b),
        },
    )
    files = InMemoryFiles(files={"tags.txt": b"1.0.0\n2.0.0\n"})

    result = announce.run(
        _args(tags=None, tags_file="tags.txt", out="dist"),
        registry=registry,
        index_github=index_github,
        fork_github=None,
        files=files,
        clock=FixedClock(),
    )

    assert result == ExitCode.OK
    committed = parse_package_root(files.read_bytes(f"dist/{_ROOT_PATH}"))  # type: ignore[arg-type]
    assert set(committed.tags) == {"1.0.0", "2.0.0"}


# --- --out: local write mode --------------------------------------------


def test_out_mode_writes_root_and_cas_files_locally() -> None:
    manifest_digest_a = "sha256:" + "1" * 64
    existing_content = _observed_content_digest("3.28.1", manifest_digest_a)
    current = _root({"3.28.1": TagEntry(content=existing_content, observed="T0")})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    manifest_digest_b = "sha256:" + "2" * 64
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1", "3.29.0"]},
        manifests={
            (_REPO, "3.28.1"): _manifest(manifest_digest_a),
            (_REPO, "3.29.0"): _manifest(manifest_digest_b),
        },
    )
    files = InMemoryFiles()

    result = announce.run(
        _args(tags="3.28.1,3.29.0", out="dist"),
        registry=registry,
        index_github=index_github,
        fork_github=None,
        files=files,
        clock=FixedClock(fixed="T1"),
    )

    assert result == ExitCode.OK
    committed_root = parse_package_root(files.read_bytes(f"dist/{_ROOT_PATH}"))  # type: ignore[arg-type]
    assert set(committed_root.tags) == {"3.28.1", "3.29.0"}
    assert committed_root.tags["3.29.0"].observed == "T1"
    cas_paths = [path for path in files.files if path.startswith(f"dist/p/{_NS}/{_PKG}/o/sha256/")]
    assert len(cas_paths) == 2  # one new object per tag (both distinct architectures/digests)
    # index_github is never written to in --out mode.
    assert set(index_github.files) == {(_ROOT_PATH, "main")}


def test_out_mode_curated_set_drops_tags_not_announced() -> None:
    # Owner curation: absent from the curated set means removed, exactly
    # `core/regenerate.py`'s existing "observations are the universe"
    # semantics (no core change needed for owner-curated add/remove).
    stale_digest = "sha256:" + "0" * 64
    current = _root({"legacy": TagEntry(content=stale_digest, observed="T0")})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    manifest_digest = "sha256:" + "1" * 64
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )
    files = InMemoryFiles()

    announce.run(
        _args(tags="3.28.1", out="dist"),
        registry=registry,
        index_github=index_github,
        fork_github=None,
        files=files,
        clock=FixedClock(),
    )

    committed_root = parse_package_root(files.read_bytes(f"dist/{_ROOT_PATH}"))  # type: ignore[arg-type]
    assert "legacy" not in committed_root.tags
    assert "3.28.1" in committed_root.tags


# --- desc regeneration: readme + logo CAS writes, extension sniffing -------


def test_desc_change_writes_readme_only_when_no_logo_layer() -> None:
    readme_bytes = b"# CMake\n"
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    manifest_digest = "sha256:" + "1" * 64
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={
            (_REPO, "3.28.1"): _manifest(manifest_digest),
            (_REPO, "__ocx.desc"): {
                "annotations": {
                    "org.opencontainers.image.title": "CMake",
                    "org.opencontainers.image.description": "Build tool",
                },
                "layers": [{"mediaType": "application/markdown", "digest": "sha256:" + "e" * 64}],
            },
        },
        desc_digests={_REPO: "sha256:" + "d" * 64},
        blobs={(_REPO, "sha256:" + "e" * 64): readme_bytes},
    )
    files = InMemoryFiles()

    result = announce.run(
        _args(tags="3.28.1", out="dist"),
        registry=registry,
        index_github=index_github,
        fork_github=None,
        files=files,
        clock=FixedClock(),
    )

    assert result == ExitCode.OK
    committed_root = parse_package_root(files.read_bytes(f"dist/{_ROOT_PATH}"))  # type: ignore[arg-type]
    assert committed_root.desc is not None
    readme_hex = committed_root.desc.readme.removeprefix("sha256:")  # type: ignore[union-attr]
    assert files.files[f"dist/p/{_NS}/{_PKG}/o/sha256/{readme_hex}.md"] == readme_bytes


def test_desc_change_writes_png_logo_with_sniffed_extension() -> None:
    readme_bytes = b"# CMake\n"
    logo_bytes = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    manifest_digest = "sha256:" + "1" * 64
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={
            (_REPO, "3.28.1"): _manifest(manifest_digest),
            (_REPO, "__ocx.desc"): {
                "annotations": {
                    "org.opencontainers.image.title": "glab",
                    "org.opencontainers.image.description": "GitLab CLI",
                },
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:" + "1" * 64},
                    {"mediaType": "image/png", "digest": "sha256:" + "2" * 64},
                ],
            },
        },
        desc_digests={_REPO: "sha256:" + "f" * 64},
        blobs={
            (_REPO, "sha256:" + "1" * 64): readme_bytes,
            (_REPO, "sha256:" + "2" * 64): logo_bytes,
        },
    )
    files = InMemoryFiles()

    announce.run(
        _args(tags="3.28.1", out="dist"),
        registry=registry,
        index_github=index_github,
        fork_github=None,
        files=files,
        clock=FixedClock(),
    )

    committed_root = parse_package_root(files.read_bytes(f"dist/{_ROOT_PATH}"))  # type: ignore[arg-type]
    assert committed_root.desc is not None
    logo_hex = committed_root.desc.logo.removeprefix("sha256:")  # type: ignore[union-attr]
    assert files.files[f"dist/p/{_NS}/{_PKG}/o/sha256/{logo_hex}.png"] == logo_bytes


def test_desc_change_writes_svg_logo_with_sniffed_extension() -> None:
    readme_bytes = b"# CMake\n"
    logo_bytes = b"<svg></svg>"
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    manifest_digest = "sha256:" + "1" * 64
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={
            (_REPO, "3.28.1"): _manifest(manifest_digest),
            (_REPO, "__ocx.desc"): {
                "annotations": {
                    "org.opencontainers.image.title": "cmake",
                    "org.opencontainers.image.description": "Build tool",
                },
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:" + "3" * 64},
                    {"mediaType": "image/svg+xml", "digest": "sha256:" + "4" * 64},
                ],
            },
        },
        desc_digests={_REPO: "sha256:" + "9" * 64},
        blobs={
            (_REPO, "sha256:" + "3" * 64): readme_bytes,
            (_REPO, "sha256:" + "4" * 64): logo_bytes,
        },
    )
    files = InMemoryFiles()

    announce.run(
        _args(tags="3.28.1", out="dist"),
        registry=registry,
        index_github=index_github,
        fork_github=None,
        files=files,
        clock=FixedClock(),
    )

    committed_root = parse_package_root(files.read_bytes(f"dist/{_ROOT_PATH}"))  # type: ignore[arg-type]
    assert committed_root.desc is not None
    logo_hex = committed_root.desc.logo.removeprefix("sha256:")  # type: ignore[union-attr]
    assert files.files[f"dist/p/{_NS}/{_PKG}/o/sha256/{logo_hex}.svg"] == logo_bytes


# --- --yank / --unyank ---------------------------------------------------


def test_yank_marks_tag_yanked() -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )
    files = InMemoryFiles()

    announce.run(
        _args(tags="3.28.1", out="dist", yank=["3.28.1"], yank_reason="cve-2026-0001"),
        registry=registry,
        index_github=index_github,
        fork_github=None,
        files=files,
        clock=FixedClock(fixed="T1"),
    )

    committed_root = parse_package_root(files.read_bytes(f"dist/{_ROOT_PATH}"))  # type: ignore[arg-type]
    yank = committed_root.tags["3.28.1"].yanked
    assert yank is not None
    assert yank.reason == "cve-2026-0001"
    assert yank.at == "T1"


def test_unyank_clears_existing_marker() -> None:
    manifest_digest = "sha256:" + "1" * 64
    content_digest = _observed_content_digest("3.28.1", manifest_digest)
    current = _root(
        {
            "3.28.1": TagEntry(
                content=content_digest, observed="T0", yanked=Yank(reason="old", at="T0")
            )
        }
    )
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )
    files = InMemoryFiles()

    announce.run(
        _args(tags="3.28.1", out="dist", unyank=["3.28.1"]),
        registry=registry,
        index_github=index_github,
        fork_github=None,
        files=files,
        clock=FixedClock(),
    )

    committed_root = parse_package_root(files.read_bytes(f"dist/{_ROOT_PATH}"))  # type: ignore[arg-type]
    assert committed_root.tags["3.28.1"].yanked is None


def test_yank_tag_not_in_curated_set_raises() -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )

    with pytest.raises(ValidationError, match="not in the curated tag set"):
        announce.run(
            _args(tags="3.28.1", out="dist", yank=["9.9.9"]),
            registry=registry,
            index_github=index_github,
            fork_github=None,
            files=InMemoryFiles(),
            clock=FixedClock(),
        )


def test_unyank_tag_not_in_curated_set_raises() -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )

    with pytest.raises(ValidationError, match="not in the curated tag set"):
        announce.run(
            _args(tags="3.28.1", out="dist", unyank=["9.9.9"]),
            registry=registry,
            index_github=index_github,
            fork_github=None,
            files=InMemoryFiles(),
            clock=FixedClock(),
        )


def test_same_tag_in_yank_and_unyank_raises() -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )

    with pytest.raises(ValidationError, match="both --yank and --unyank"):
        announce.run(
            _args(tags="3.28.1", out="dist", yank=["3.28.1"], unyank=["3.28.1"]),
            registry=registry,
            index_github=index_github,
            fork_github=None,
            files=InMemoryFiles(),
            clock=FixedClock(),
        )


# --- --fork: cross-repo PR mode ------------------------------------------


def test_fork_mode_commits_to_fork_and_opens_pr_against_index_repo() -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    index_github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(current)},
        refs={"main": "index-main-sha"},
    )
    fork_github = FakeGitHub(refs={"main": "fork-main-sha"})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )

    result = announce.run(
        _args(tags="3.28.1", out=None, fork="alice/index"),
        registry=registry,
        index_github=index_github,
        fork_github=fork_github,
        files=InMemoryFiles(),
        clock=FixedClock(),
    )

    assert result == ExitCode.OK
    assert (_ROOT_PATH, _BRANCH) in fork_github.files
    assert index_github.pull_requests == {f"alice:{_BRANCH}": 1}
    committed_root = parse_package_root(fork_github.files[(_ROOT_PATH, _BRANCH)])
    assert "3.28.1" in committed_root.tags


def test_fork_mode_reuses_existing_announce_branch_as_commit_base() -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    fork_github = FakeGitHub(refs={"main": "fork-main-sha", _BRANCH: "stale-branch-sha"})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )

    # If base_sha were sourced from "main" instead of the already-open
    # branch, FakeGitHub.commit_files would raise TransientError (stale
    # base) — reaching ExitCode.OK proves the existing branch was reused.
    result = announce.run(
        _args(tags="3.28.1", out=None, fork="alice/index"),
        registry=registry,
        index_github=index_github,
        fork_github=fork_github,
        files=InMemoryFiles(),
        clock=FixedClock(),
    )

    assert result == ExitCode.OK


def test_fork_mode_missing_base_ref_raises_validation_error() -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    fork_github = FakeGitHub()  # no refs at all
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )

    with pytest.raises(ValidationError, match="main"):
        announce.run(
            _args(tags="3.28.1", out=None, fork="alice/index"),
            registry=registry,
            index_github=index_github,
            fork_github=fork_github,
            files=InMemoryFiles(),
            clock=FixedClock(),
        )


def test_fork_mode_never_writes_to_index_repo_files() -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    index_github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    fork_github = FakeGitHub(refs={"main": "fork-main-sha"})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )

    announce.run(
        _args(tags="3.28.1", out=None, fork="alice/index"),
        registry=registry,
        index_github=index_github,
        fork_github=fork_github,
        files=InMemoryFiles(),
        clock=FixedClock(),
    )

    assert set(index_github.files) == {(_ROOT_PATH, "main")}
