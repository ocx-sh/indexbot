from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pytest

from indexbot.cli import announce
from indexbot.core.diff import ChangeClass
from indexbot.core.observe import observe
from indexbot.core.validate_entry import parse_package_root, serialize_package_root
from indexbot.errors import AnomalyError, TransientError, ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import Desc, ManifestFetch, Owner, OwnershipProbeResult, PackageRoot, TagEntry
from tests.fakes import FakeGitHub, FakeRegistry, FixedClock

_NS = "kitware"
_PKG = "cmake"
_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_ROOT_PATH = f"p/{_NS}/{_PKG}.json"
_BRANCH = f"indexbot-announce-{_NS}-{_PKG}"
_OWNER = Owner(github="alice", github_id=1)


@dataclass
class _RaisingRegistry:
    """Minimal standalone `RegistryPort` double proving a code path never
    reaches the network — not `FakeRegistry` (consume-only, never edited,
    per this work package's instructions). Mirrors `tests/core/test_observe.py`'s
    `_RaisingRegistry` precedent exactly."""

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


def _args(*, package: str | None = None, validate_only: bool = False) -> argparse.Namespace:
    return argparse.Namespace(package=package, validate_only=validate_only)


def _root(
    tags: dict[str, TagEntry], *, desc: Desc | None = None, repository: str = _REPO
) -> PackageRoot:
    return PackageRoot(
        name=f"ocx.sh/{_NS}/{_PKG}",
        repository=repository,
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=desc,
        tags=dict(tags),
    )


def _manifest(digest: str) -> dict[str, object]:
    return {"platform": {"architecture": "amd64", "os": "linux"}, "digest": digest}


def _observed_content_digest(tag: str, manifest_digest: str) -> str:
    """The exact `Observation.content_digest` `observe()` computes for a
    single-tag, single-platform manifest — used to seed a committed root's
    `TagEntry.content` so a later `observe()` call over the same registry
    state reproduces byte-identical output (the no-op/refresh fixtures)."""
    registry = FakeRegistry(
        tags={_REPO: [tag]}, manifests={(_REPO, tag): _manifest(manifest_digest)}
    )
    (observation,) = observe(_REPO, registry)
    return observation.content_digest


def _read_outputs(path: Path) -> dict[str, str]:
    """Parse `$GITHUB_OUTPUT`'s multiline delimiter form back into a dict."""
    outputs: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        name, delimiter = lines[index].split("<<", 1)
        index += 1
        value_lines: list[str] = []
        while lines[index] != delimiter:
            value_lines.append(lines[index])
            index += 1
        outputs[name] = "\n".join(value_lines)
        index += 1
    return outputs


# --- --validate-only: no port ever touched (announce.yml's unprivileged job) ------


def test_validate_only_with_package_override_writes_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    result = announce.run(
        _args(package=f"{_NS}/{_PKG}", validate_only=True),
        registry=_RaisingRegistry(),
        github=FakeGitHub(),
        clock=FixedClock(),
    )

    assert result == ExitCode.OK
    assert _read_outputs(output_file)["result"] == "validated"


def test_validate_only_reads_package_id_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PACKAGE_ID", f"{_NS}/{_PKG}")
    output_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    result = announce.run(
        _args(validate_only=True),
        registry=_RaisingRegistry(),
        github=FakeGitHub(),
        clock=FixedClock(),
    )

    assert result == ExitCode.OK
    assert _read_outputs(output_file)["result"] == "validated"


def test_validate_only_rejects_malformed_package_override() -> None:
    with pytest.raises(ValidationError):
        announce.run(
            _args(package="BAD ID", validate_only=True),
            registry=_RaisingRegistry(),
            github=FakeGitHub(),
            clock=FixedClock(),
        )


def test_missing_package_id_env_raises_before_validate_only_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PACKAGE_ID", raising=False)
    with pytest.raises(ValidationError, match="is not set"):
        announce.run(
            _args(validate_only=True),
            registry=_RaisingRegistry(),
            github=FakeGitHub(),
            clock=FixedClock(),
        )


# --- unclaimed namespace / SSRF ordering / transient propagation -----------------


def test_unclaimed_namespace_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="unclaimed"):
        announce.run(
            _args(package=f"{_NS}/{_PKG}"),
            registry=_RaisingRegistry(),
            github=FakeGitHub(),
            clock=FixedClock(),
        )


def test_repository_allowlist_checked_before_any_registry_call() -> None:
    current = _root({}, repository="oci://evil.example.com/ns/pkg")
    github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})

    with pytest.raises(ValidationError, match="allowlisted"):
        announce.run(
            _args(package=f"{_NS}/{_PKG}"),
            registry=_RaisingRegistry(),
            github=github,
            clock=FixedClock(),
        )


def test_transient_error_from_observe_propagates_uncaught() -> None:
    current = _root({})
    github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})

    with pytest.raises(TransientError) as exc_info:
        announce.run(
            _args(package=f"{_NS}/{_PKG}"),
            registry=_RaisingRegistry(),
            github=github,
            clock=FixedClock(),
        )
    assert exc_info.value.exit_code == ExitCode.TRANSIENT


# --- anomaly: pinned-tag mutation blocks before any write -------------------------


def test_pinned_tag_mutation_raises_anomaly_before_any_write() -> None:
    committed_digest = "sha256:" + "a" * 64
    current = _root({"3.28.1": TagEntry(content=committed_digest, observed="T0")})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(current)}, refs={"main": "main-sha"}
    )
    fresh_manifest_digest = "sha256:" + "b" * 64
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(fresh_manifest_digest)}
    )

    with pytest.raises(AnomalyError) as exc_info:
        announce.run(
            _args(package=f"{_NS}/{_PKG}"), registry=registry, github=github, clock=FixedClock()
        )

    assert exc_info.value.exit_code == ExitCode.ANOMALY
    assert github.pull_requests == {}
    assert set(github.files) == {(_ROOT_PATH, "main")}


# --- no-op: identical registry state produces no diff -----------------------------


def test_no_op_when_nothing_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_digest = "sha256:" + "1" * 64
    content_digest = _observed_content_digest("3.28.1", manifest_digest)
    current = _root({"3.28.1": TagEntry(content=content_digest, observed="T0")})
    github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(current)})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )
    output_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    result = announce.run(
        _args(package=f"{_NS}/{_PKG}"), registry=registry, github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK
    assert _read_outputs(output_file)["result"] == "no-op"
    assert github.pull_requests == {}


# --- applied: new tag -> refresh-class PR, labeled, auto-merged -------------------


def test_new_tag_applies_refresh_pr_labeled_and_auto_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_digest_a = "sha256:" + "1" * 64
    existing_content = _observed_content_digest("3.28.1", manifest_digest_a)
    current = _root({"3.28.1": TagEntry(content=existing_content, observed="T0")})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(current)}, refs={"main": "main-sha-1"}
    )
    manifest_digest_b = "sha256:" + "2" * 64
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1", "3.29.0"]},
        manifests={
            (_REPO, "3.28.1"): _manifest(manifest_digest_a),
            (_REPO, "3.29.0"): _manifest(manifest_digest_b),
        },
    )
    output_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    result = announce.run(
        _args(package=f"{_NS}/{_PKG}"),
        registry=registry,
        github=github,
        clock=FixedClock(fixed="T1"),
    )

    assert result == ExitCode.OK
    outputs = _read_outputs(output_file)
    assert outputs["result"] == "applied"
    pr_number = int(outputs["pr_number"])
    assert github.labels[pr_number] == ["refresh"]
    assert pr_number in github.auto_merge_enabled

    committed_root = parse_package_root(github.files[(_ROOT_PATH, _BRANCH)])
    assert "3.29.0" in committed_root.tags
    cas_keys = [
        path
        for path, branch in github.files
        if branch == _BRANCH
        and path.startswith(f"p/{_NS}/{_PKG}/o/sha256/")
        and path.endswith(".json")
    ]
    assert len(cas_keys) == 1
    assert github.refs[_BRANCH] != "main-sha-1"


def test_reuses_existing_announce_branch_as_commit_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_digest_a = "sha256:" + "1" * 64
    existing_content = _observed_content_digest("3.28.1", manifest_digest_a)
    current = _root({"3.28.1": TagEntry(content=existing_content, observed="T0")})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(current)},
        refs={"main": "main-sha-should-not-be-used", _BRANCH: "stale-branch-sha"},
    )
    manifest_digest_b = "sha256:" + "2" * 64
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1", "3.29.0"]},
        manifests={
            (_REPO, "3.28.1"): _manifest(manifest_digest_a),
            (_REPO, "3.29.0"): _manifest(manifest_digest_b),
        },
    )
    output_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    # If the base_sha were sourced from "main" instead of the already-open
    # branch, FakeGitHub.commit_files would raise TransientError (stale
    # base) — reaching ExitCode.OK proves the existing branch was reused.
    result = announce.run(
        _args(package=f"{_NS}/{_PKG}"), registry=registry, github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK


def test_missing_base_ref_raises_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_digest = "sha256:" + "1" * 64
    current = _root({})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(current)}
    )  # no refs at all
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): _manifest(manifest_digest)}
    )

    with pytest.raises(ValidationError, match="main"):
        announce.run(
            _args(package=f"{_NS}/{_PKG}"), registry=registry, github=github, clock=FixedClock()
        )


def test_human_review_class_is_labeled_but_not_auto_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`classify_change` is structurally always `"refresh"` when reached from
    `announce`'s own pipeline (`regenerate` carries every governance/yanked
    field `classify_change` inspects over verbatim from `current` — see
    `open_questions`). Monkeypatching the name as imported into
    `indexbot.cli.announce` (same technique as `tests/cli/test_common.py`'s
    `_random_delimiter` patch) exercises the safety-net branch anyway,
    without touching `tests/fakes/` or `core/diff.py`.
    """
    manifest_digest_a = "sha256:" + "1" * 64
    existing_content = _observed_content_digest("3.28.1", manifest_digest_a)
    current = _root({"3.28.1": TagEntry(content=existing_content, observed="T0")})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(current)}, refs={"main": "main-sha-1"}
    )
    manifest_digest_b = "sha256:" + "2" * 64
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1", "3.29.0"]},
        manifests={
            (_REPO, "3.28.1"): _manifest(manifest_digest_a),
            (_REPO, "3.29.0"): _manifest(manifest_digest_b),
        },
    )

    def _force_human_review(before: PackageRoot | None, after: PackageRoot) -> ChangeClass:
        del before, after
        return "human-review-required"

    output_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(announce, "classify_change", _force_human_review)

    result = announce.run(
        _args(package=f"{_NS}/{_PKG}"), registry=registry, github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK
    outputs = _read_outputs(output_file)
    pr_number = int(outputs["pr_number"])
    assert github.labels[pr_number] == ["human-review-required"]
    assert pr_number not in github.auto_merge_enabled


# --- desc regeneration: readme + logo CAS writes, extension sniffing --------------


def test_desc_change_writes_readme_only_when_no_logo_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readme_bytes = b"# CMake\n"
    current = _root({})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(current)}, refs={"main": "main-sha-1"}
    )
    registry = FakeRegistry(
        desc_digests={_REPO: "sha256:" + "d" * 64},
        manifests={
            (_REPO, "__ocx.desc"): {
                "annotations": {
                    "org.opencontainers.image.title": "CMake",
                    "org.opencontainers.image.description": "Build tool",
                },
                "layers": [{"mediaType": "application/markdown", "digest": "sha256:" + "e" * 64}],
            }
        },
        blobs={(_REPO, "sha256:" + "e" * 64): readme_bytes},
    )
    output_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    result = announce.run(
        _args(package=f"{_NS}/{_PKG}"), registry=registry, github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK
    committed_root = parse_package_root(github.files[(_ROOT_PATH, _BRANCH)])
    assert committed_root.desc is not None
    readme_hex = committed_root.desc.readme.removeprefix("sha256:")  # type: ignore[union-attr]
    assert github.files[(f"p/{_NS}/{_PKG}/o/sha256/{readme_hex}.md", _BRANCH)] == readme_bytes
    logo_paths = [
        path
        for path, branch in github.files
        if branch == _BRANCH
        and "/o/sha256/" in path
        and not path.endswith(".md")
        and not path.endswith(".json")
    ]
    assert logo_paths == []


def test_desc_change_writes_png_logo_with_sniffed_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readme_bytes = b"# CMake\n"
    logo_bytes = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
    current = _root({})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(current)}, refs={"main": "main-sha-1"}
    )
    registry = FakeRegistry(
        desc_digests={_REPO: "sha256:" + "f" * 64},
        manifests={
            (_REPO, "__ocx.desc"): {
                "annotations": {
                    "org.opencontainers.image.title": "glab",
                    "org.opencontainers.image.description": "GitLab CLI",
                },
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:" + "1" * 64},
                    {"mediaType": "image/png", "digest": "sha256:" + "2" * 64},
                ],
            }
        },
        blobs={
            (_REPO, "sha256:" + "1" * 64): readme_bytes,
            (_REPO, "sha256:" + "2" * 64): logo_bytes,
        },
    )
    output_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    result = announce.run(
        _args(package=f"{_NS}/{_PKG}"), registry=registry, github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK
    committed_root = parse_package_root(github.files[(_ROOT_PATH, _BRANCH)])
    assert committed_root.desc is not None
    logo_hex = committed_root.desc.logo.removeprefix("sha256:")  # type: ignore[union-attr]
    assert github.files[(f"p/{_NS}/{_PKG}/o/sha256/{logo_hex}.png", _BRANCH)] == logo_bytes


def test_desc_change_writes_svg_logo_with_sniffed_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readme_bytes = b"# CMake\n"
    logo_bytes = b"<svg></svg>"
    current = _root({})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(current)}, refs={"main": "main-sha-1"}
    )
    registry = FakeRegistry(
        desc_digests={_REPO: "sha256:" + "9" * 64},
        manifests={
            (_REPO, "__ocx.desc"): {
                "annotations": {
                    "org.opencontainers.image.title": "cmake",
                    "org.opencontainers.image.description": "Build tool",
                },
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:" + "3" * 64},
                    {"mediaType": "image/svg+xml", "digest": "sha256:" + "4" * 64},
                ],
            }
        },
        blobs={
            (_REPO, "sha256:" + "3" * 64): readme_bytes,
            (_REPO, "sha256:" + "4" * 64): logo_bytes,
        },
    )
    output_file = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    result = announce.run(
        _args(package=f"{_NS}/{_PKG}"), registry=registry, github=github, clock=FixedClock()
    )

    assert result == ExitCode.OK
    committed_root = parse_package_root(github.files[(_ROOT_PATH, _BRANCH)])
    assert committed_root.desc is not None
    logo_hex = committed_root.desc.logo.removeprefix("sha256:")  # type: ignore[union-attr]
    assert github.files[(f"p/{_NS}/{_PKG}/o/sha256/{logo_hex}.svg", _BRANCH)] == logo_bytes
