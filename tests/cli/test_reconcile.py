from __future__ import annotations

import argparse
from dataclasses import dataclass, field

import pytest

from indexbot.cli import reconcile
from indexbot.core.observe import observe_one_tag
from indexbot.core.validate_entry import serialize_observation_object, serialize_package_root
from indexbot.errors import AnomalyError, ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import (
    Desc,
    ManifestFetch,
    Owner,
    OwnershipProbeResult,
    PackageRoot,
    TagEntry,
    Yank,
)
from tests.fakes import FakeGitHub, FakeRegistry, InMemoryFiles

_OWNER = Owner(github="alice", github_id=1)
_CMAKE_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_WIDGET_REPO = "oci://ghcr.io/ocx-contrib/widget"
_ISSUE_TITLE = "indexbot reconcile: anomalies detected"


def _args(*, package: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(command="reconcile", package=package)


def _root(
    name: str = "ocx.sh/kitware/cmake",
    repository: str = _CMAKE_REPO,
    tags: dict[str, TagEntry] | None = None,
    desc: Desc | None = None,
) -> PackageRoot:
    return PackageRoot(
        name=name,
        repository=repository,
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=desc,
        tags=dict(tags or {}),
    )


def _bare_manifest(architecture: str = "amd64") -> dict[str, object]:
    return {"platform": {"architecture": architecture, "os": "linux"}}


def _put_root(files: InMemoryFiles, namespace: str, package: str, root: PackageRoot) -> None:
    files.write_bytes(f"p/{namespace}/{package}.json", serialize_package_root(root))


def _committed_tag(
    registry: FakeRegistry, repository: str, tag: str, *, architecture: str = "amd64"
) -> tuple[TagEntry, bytes]:
    """A `TagEntry` + its CAS object bytes that independently re-derive to
    the exact same content digest from `registry` — the clean baseline every
    test mutates one field of."""
    registry.tags.setdefault(repository, []).append(tag)
    registry.manifests[(repository, tag)] = _bare_manifest(architecture)
    observation = observe_one_tag(repository, tag, registry)
    assert observation is not None
    object_bytes = serialize_observation_object(observation.object)
    entry = TagEntry(content=observation.content_digest, observed="2026-07-17T00:00:00Z")
    return entry, object_bytes


def _put_cas(
    files: InMemoryFiles,
    namespace: str,
    package: str,
    digest: str,
    content: bytes,
    *,
    ext: str = "json",
) -> None:
    hex_part = digest.removeprefix("sha256:")
    files.write_bytes(f"p/{namespace}/{package}/o/sha256/{hex_part}.{ext}", content)


@dataclass
class _GhostFiles:
    """`FilePort` double: `list_files` reports a root that `read_bytes` can
    no longer find — the list-then-read race `_verify_one` tolerates."""

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
    """A `RegistryPort` whose every method raises — proves `_verify_one`
    never reaches the network for a committed root whose `repository` isn't
    allowlisted (SSRF ordering, ADR-4 BD-1)."""

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


# --- add_arguments -----------------------------------------------------


def test_add_arguments_registers_package_scope() -> None:
    parser = argparse.ArgumentParser()
    reconcile.add_arguments(parser)
    parsed = parser.parse_args(["--package", "kitware/cmake"])
    assert parsed.package == "kitware/cmake"


def test_add_arguments_package_defaults_to_none() -> None:
    parser = argparse.ArgumentParser()
    reconcile.add_arguments(parser)
    parsed = parser.parse_args([])
    assert parsed.package is None


# --- basic sweep behavior -------------------------------------------------


def test_unallowlisted_repository_raises_before_any_registry_call() -> None:
    files = InMemoryFiles()
    _put_root(files, "kitware", "cmake", _root(repository="oci://evil.example.com/x/y"))

    with pytest.raises(ValidationError, match="G-03"):
        reconcile.run(_args(), files=files, registry=_PoisonRegistry(), github=FakeGitHub())


def test_empty_index_is_a_noop() -> None:
    result = reconcile.run(
        _args(), files=InMemoryFiles(), registry=FakeRegistry(), github=FakeGitHub()
    )
    assert result == ExitCode.OK


def test_missing_args_attributes_default_to_full_run() -> None:
    bare_args = argparse.Namespace(command="reconcile")
    result = reconcile.run(
        bare_args, files=InMemoryFiles(), registry=FakeRegistry(), github=FakeGitHub()
    )
    assert result == ExitCode.OK


def test_clean_package_is_a_noop_and_never_writes() -> None:
    files = InMemoryFiles()
    registry = FakeRegistry()
    entry, object_bytes = _committed_tag(registry, _CMAKE_REPO, "3.28.1")
    _put_root(files, "kitware", "cmake", _root(tags={"3.28.1": entry}))
    _put_cas(files, "kitware", "cmake", entry.content, object_bytes)
    github = FakeGitHub()

    result = reconcile.run(_args(), files=files, registry=registry, github=github)

    assert result == ExitCode.OK
    assert github.issues == {}


def test_package_scope_filters_to_one_package() -> None:
    files = InMemoryFiles()
    registry = FakeRegistry()
    entry_a, bytes_a = _committed_tag(registry, _CMAKE_REPO, "3.28.1")
    _put_root(files, "kitware", "cmake", _root(tags={"3.28.1": entry_a}))
    _put_cas(files, "kitware", "cmake", entry_a.content, bytes_a)
    # A second package with a dangling CAS reference (would escalate) —
    # proves scoping actually excludes it from the sweep, not just from the
    # report text.
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

    result = reconcile.run(
        _args(package="kitware/cmake"), files=files, registry=registry, github=FakeGitHub()
    )

    assert result == ExitCode.OK


def test_ghost_root_vanished_between_list_and_read_is_skipped() -> None:
    files = _GhostFiles(listed=["p/kitware/cmake.json"])
    result = reconcile.run(_args(), files=files, registry=FakeRegistry(), github=FakeGitHub())
    assert result == ExitCode.OK


# --- pinned-tag mutation (core/anomaly.py, reused verbatim) -> escalates --


def test_pinned_tag_mutation_escalates_to_anomaly() -> None:
    files = InMemoryFiles()
    registry = FakeRegistry()
    committed_digest = "sha256:" + "a" * 64
    _put_root(
        files,
        "kitware",
        "cmake",
        _root(tags={"3.28.1": TagEntry(content=committed_digest, observed="T0")}),
    )
    _put_cas(files, "kitware", "cmake", committed_digest, b'{"platforms":[]}')
    # Registry now resolves "3.28.1" to different content entirely.
    _committed_tag(registry, _CMAKE_REPO, "3.28.1")
    github = FakeGitHub()

    with pytest.raises(AnomalyError, match="pinned-tag-mutation"):
        reconcile.run(_args(), files=files, registry=registry, github=github)

    assert "pinned-tag-mutation" in github.issues[_ISSUE_TITLE][1]


# --- verify_claims findings: subset semantics + escalation disposition ----


def test_floating_tag_drift_does_not_escalate() -> None:
    # "latest" is not a pinned (full-release) version — a digest-mismatch
    # here is the expected cascade-push behavior (ADR-1 D2/D3), never an
    # anomaly.
    files = InMemoryFiles()
    registry = FakeRegistry()
    committed_digest = "sha256:" + "a" * 64
    _put_root(
        files,
        "kitware",
        "cmake",
        _root(tags={"latest": TagEntry(content=committed_digest, observed="T0")}),
    )
    _put_cas(files, "kitware", "cmake", committed_digest, b'{"platforms":[]}')
    registry.tags[_CMAKE_REPO] = ["latest"]
    registry.manifests[(_CMAKE_REPO, "latest")] = _bare_manifest(architecture="arm64")

    result = reconcile.run(_args(), files=files, registry=registry, github=FakeGitHub())

    assert result == ExitCode.OK


def test_yanked_vanished_tag_does_not_escalate() -> None:
    # ADR-6 FP-2/FP-3: yank is grace — an explicit owner-authorized exemption
    # from the registry-existence check, so a yanked tag vanishing entirely
    # is not itself an anomaly.
    files = InMemoryFiles()
    registry = FakeRegistry()  # no tags registered at all -> tag no longer resolves
    committed_digest = "sha256:" + "a" * 64
    yanked_entry = TagEntry(
        content=committed_digest, observed="T0", yanked=Yank(reason="cve", at="T0")
    )
    _put_root(files, "kitware", "cmake", _root(tags={"3.28.1": yanked_entry}))
    _put_cas(files, "kitware", "cmake", committed_digest, b'{"platforms":[]}')

    result = reconcile.run(_args(), files=files, registry=registry, github=FakeGitHub())

    assert result == ExitCode.OK


def test_non_yanked_vanished_tag_escalates_to_anomaly() -> None:
    # ADR-6 FP-2/FP-3: everything else vanished-upstream is an anomaly, not
    # a silent drop.
    files = InMemoryFiles()
    registry = FakeRegistry()  # no tags registered at all -> tag no longer resolves
    committed_digest = "sha256:" + "a" * 64
    _put_root(
        files,
        "kitware",
        "cmake",
        _root(tags={"3.28.1": TagEntry(content=committed_digest, observed="T0")}),
    )
    _put_cas(files, "kitware", "cmake", committed_digest, b'{"platforms":[]}')
    github = FakeGitHub()

    with pytest.raises(AnomalyError, match="tag-missing-upstream"):
        reconcile.run(_args(), files=files, registry=registry, github=github)

    assert "tag-missing-upstream" in github.issues[_ISSUE_TITLE][1]


def test_dangling_cas_reference_escalates() -> None:
    files = InMemoryFiles()
    registry = FakeRegistry()
    entry, _object_bytes = _committed_tag(registry, _CMAKE_REPO, "3.28.1")
    _put_root(files, "kitware", "cmake", _root(tags={"3.28.1": entry}))
    # No CAS file written at all for entry.content.
    github = FakeGitHub()

    with pytest.raises(AnomalyError, match="cas-object-missing"):
        reconcile.run(_args(), files=files, registry=registry, github=github)


def test_tampered_cas_object_escalates() -> None:
    files = InMemoryFiles()
    registry = FakeRegistry()
    entry, _object_bytes = _committed_tag(registry, _CMAKE_REPO, "3.28.1")
    _put_root(files, "kitware", "cmake", _root(tags={"3.28.1": entry}))
    _put_cas(files, "kitware", "cmake", entry.content, b"tampered bytes")

    with pytest.raises(AnomalyError, match="cas-object-hash-mismatch"):
        reconcile.run(_args(), files=files, registry=registry, github=FakeGitHub())


def test_desc_blob_hash_mismatch_escalates() -> None:
    files = InMemoryFiles()
    registry = FakeRegistry()
    readme_digest = "sha256:" + "e" * 64
    desc = Desc(digest="sha256:" + "d" * 64, title="CMake", description="x", readme=readme_digest)
    _put_root(files, "kitware", "cmake", _root(tags={}, desc=desc))
    _put_cas(files, "kitware", "cmake", readme_digest, b"tampered readme", ext="md")

    with pytest.raises(AnomalyError, match="desc-blob-hash-mismatch"):
        reconcile.run(_args(), files=files, registry=registry, github=FakeGitHub())


def test_desc_blob_missing_escalates() -> None:
    files = InMemoryFiles()
    registry = FakeRegistry()
    readme_digest = "sha256:" + "e" * 64
    desc = Desc(digest="sha256:" + "d" * 64, title="CMake", description="x", readme=readme_digest)
    _put_root(files, "kitware", "cmake", _root(tags={}, desc=desc))
    # No CAS file written for the readme digest at all.

    with pytest.raises(AnomalyError, match="desc-blob-missing"):
        reconcile.run(_args(), files=files, registry=registry, github=FakeGitHub())


# --- partial-success: clean + anomalous packages together -----------------


def test_clean_and_anomalous_packages_both_reported() -> None:
    files = InMemoryFiles()
    registry = FakeRegistry()
    clean_entry, clean_bytes = _committed_tag(registry, _CMAKE_REPO, "3.28.1")
    _put_root(files, "kitware", "cmake", _root(tags={"3.28.1": clean_entry}))
    _put_cas(files, "kitware", "cmake", clean_entry.content, clean_bytes)

    # Tag genuinely resolves upstream (so this isn't "tag-missing-upstream"),
    # but its CAS object was never committed locally — a dangling reference.
    dangling_entry, _dangling_bytes = _committed_tag(registry, _WIDGET_REPO, "1.0.0")
    _put_root(
        files,
        "acme",
        "widget",
        _root(name="ocx.sh/acme/widget", repository=_WIDGET_REPO, tags={"1.0.0": dangling_entry}),
    )
    github = FakeGitHub()

    with pytest.raises(AnomalyError) as exc_info:
        reconcile.run(_args(), files=files, registry=registry, github=github)

    assert "acme/widget" in str(exc_info.value)
    assert "kitware/cmake" not in str(exc_info.value)
    assert github.issues[_ISSUE_TITLE][0] == 1  # exactly one issue, not one per package


def test_anomaly_issue_is_idempotent_across_repeated_runs() -> None:
    files = InMemoryFiles()
    registry = FakeRegistry()
    entry, _object_bytes = _committed_tag(registry, _CMAKE_REPO, "3.28.1")
    _put_root(files, "kitware", "cmake", _root(tags={"3.28.1": entry}))
    github = FakeGitHub()

    with pytest.raises(AnomalyError):
        reconcile.run(_args(), files=files, registry=registry, github=github)
    with pytest.raises(AnomalyError):
        reconcile.run(_args(), files=files, registry=registry, github=github)

    assert len(github.issues) == 1
