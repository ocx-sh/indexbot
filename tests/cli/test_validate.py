from __future__ import annotations

import argparse
import hashlib

import pytest

from indexbot.cli import validate
from indexbot.core.validate_entry import serialize_observation_object, serialize_package_root
from indexbot.exit_codes import ExitCode
from indexbot.model import (
    Desc,
    ManifestFetch,
    ObservationObject,
    OciPlatform,
    Owner,
    OwnershipProbeResult,
    PackageRoot,
    PlatformEntry,
    TagEntry,
)
from tests.fakes import FakeRegistry, InMemoryFiles

_NAMESPACE = "kitware"
_PACKAGE = "cmake"
_PATH = f"p/{_NAMESPACE}/{_PACKAGE}.json"
_REPOSITORY = "oci://ghcr.io/ocx-contrib/cmake"
_NAME = f"ocx.sh/{_NAMESPACE}/{_PACKAGE}"
_PLATFORM_DIGEST = "sha256:" + "1" * 64


def _cas_path(digest: str, *, ext: str = "json") -> str:
    return f"p/{_NAMESPACE}/{_PACKAGE}/o/sha256/{digest.removeprefix('sha256:')}.{ext}"


def _observation_bytes(*, platforms: tuple[PlatformEntry, ...] | None = None) -> bytes:
    if platforms is None:
        platform = OciPlatform(architecture="amd64", os="linux")
        platforms = (PlatformEntry(platform=platform, digest=_PLATFORM_DIGEST),)
    return serialize_observation_object(ObservationObject(platforms=platforms))


def _content_digest(object_bytes: bytes) -> str:
    return f"sha256:{hashlib.sha256(object_bytes).hexdigest()}"


def _build(
    *,
    path: str = _PATH,
    name: str = _NAME,
    repository: str = _REPOSITORY,
    tags: dict[str, TagEntry] | None = None,
    desc: Desc | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> InMemoryFiles:
    """A minimal `PackageRoot`, serialized and stored at `path` — every
    failure-case test below overrides exactly the one field it needs to
    violate."""
    root = PackageRoot(
        name=name,
        repository=repository,
        owners=(Owner(github="alice", github_id=1),),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=desc,
        tags={} if tags is None else tags,
    )
    files: dict[str, bytes] = {path: serialize_package_root(root)}
    if extra_files:
        files.update(extra_files)
    return InMemoryFiles(files=files)


def _valid_package() -> tuple[InMemoryFiles, FakeRegistry]:
    """One tag, one platform, everything self-consistent and in scope — the
    baseline every online happy-path test starts from."""
    object_bytes = _observation_bytes()
    tag_digest = _content_digest(object_bytes)
    files = _build(
        tags={"3.28.1": TagEntry(content=tag_digest, observed="T0")},
        extra_files={_cas_path(tag_digest): object_bytes},
    )
    registry = FakeRegistry(
        manifests={(_REPOSITORY, _PLATFORM_DIGEST): {"schemaVersion": 2}},
        ownership={_REPOSITORY: "confirmed"},
    )
    return files, registry


def _args(
    paths: list[str], *, offline: bool = False, allow_reserved_namespace: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(
        paths=paths, offline=offline, allow_reserved_namespace=allow_reserved_namespace
    )


class _PoisonRegistry:
    """Every method raises — proves `run` never reaches the network once an
    earlier check has already rejected the file (SSRF ordering; mirrors
    `tests/test_validate_entry.py`'s `_PoisonRegistry`)."""

    def list_tags(self, repository: str) -> list[str]:
        raise AssertionError("registry.list_tags must never be called")

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        raise AssertionError("registry.get_manifest must never be called")

    def get_desc_tag_digest(self, repository: str) -> str | None:
        raise AssertionError("registry.get_desc_tag_digest must never be called")

    def get_blob(self, repository: str, digest: str) -> bytes:
        raise AssertionError("registry.get_blob must never be called")

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        raise AssertionError("registry.probe_ownership must never be called")


# --- happy paths -------------------------------------------------------


def test_run_all_checks_pass_online_exits_ok(capsys: pytest.CaptureFixture[str]) -> None:
    files, registry = _valid_package()
    result = validate.run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK
    assert f"{_PATH}: OK" in capsys.readouterr().err


def test_run_offline_skips_registry_checks_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    files, _registry = _valid_package()
    result = validate.run(_args([_PATH], offline=True), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.OK
    err = capsys.readouterr().err
    assert f"{_PATH}: WARN - G-15 registry checks skipped (--offline)" in err


def test_run_no_tags_online_passes_and_still_probes_ownership() -> None:
    files = _build(tags={})
    registry = FakeRegistry(ownership={_REPOSITORY: "confirmed"})
    result = validate.run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK


def test_run_tag_with_no_platforms_passes() -> None:
    object_bytes = _observation_bytes(platforms=())
    tag_digest = _content_digest(object_bytes)
    files = _build(
        tags={"latest": TagEntry(content=tag_digest, observed="T0")},
        extra_files={_cas_path(tag_digest): object_bytes},
    )
    registry = FakeRegistry(ownership={_REPOSITORY: "confirmed"})
    result = validate.run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK


def test_run_desc_without_readme_or_logo_passes() -> None:
    object_bytes = _observation_bytes()
    tag_digest = _content_digest(object_bytes)
    desc = Desc(digest="sha256:" + "d" * 64, title="CMake", description="Build tool")
    files = _build(
        tags={"3.28.1": TagEntry(content=tag_digest, observed="T0")},
        desc=desc,
        extra_files={_cas_path(tag_digest): object_bytes},
    )
    registry = FakeRegistry(
        manifests={(_REPOSITORY, _PLATFORM_DIGEST): {"schemaVersion": 2}},
        ownership={_REPOSITORY: "confirmed"},
    )
    result = validate.run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK


def test_run_desc_with_readme_and_logo_passes() -> None:
    object_bytes = _observation_bytes()
    tag_digest = _content_digest(object_bytes)
    readme_digest = "sha256:" + "e" * 64
    logo_digest = "sha256:" + "f" * 64
    desc = Desc(
        digest="sha256:" + "d" * 64,
        title="CMake",
        description="Build tool",
        readme=readme_digest,
        logo=logo_digest,
    )
    files = _build(
        tags={"3.28.1": TagEntry(content=tag_digest, observed="T0")},
        desc=desc,
        extra_files={
            _cas_path(tag_digest): object_bytes,
            _cas_path(readme_digest, ext="md"): b"# CMake",
            _cas_path(logo_digest, ext="svg"): b"<svg></svg>",
        },
    )
    registry = FakeRegistry(
        manifests={(_REPOSITORY, _PLATFORM_DIGEST): {"schemaVersion": 2}},
        ownership={_REPOSITORY: "confirmed"},
    )
    result = validate.run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK


def test_run_ownership_unconfirmed_warns_but_passes(capsys: pytest.CaptureFixture[str]) -> None:
    files, _registry = _valid_package()
    registry = FakeRegistry(manifests={(_REPOSITORY, _PLATFORM_DIGEST): {"schemaVersion": 2}})
    result = validate.run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK
    assert "WARN - ownership unconfirmed (G-15)" in capsys.readouterr().err


# --- validation failures (exit 1) ---------------------------------------


def test_run_missing_file_is_validation_failure() -> None:
    files = InMemoryFiles(files={})
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_json_is_validation_failure() -> None:
    files = InMemoryFiles(files={_PATH: b"not json"})
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_bad_path_shape_is_validation_failure() -> None:
    bad_path = "p/kitware.json"
    files = InMemoryFiles(files={bad_path: _build().files[_PATH]})
    result = validate.run(_args([bad_path]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_name_path_mismatch_is_validation_failure() -> None:
    files = _build(name="ocx.sh/kitware/other-tool")
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_reserved_namespace_is_validation_failure() -> None:
    path = "p/admin/cmake.json"
    files = _build(path=path, name="ocx.sh/admin/cmake")
    result = validate.run(_args([path]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_repository_not_allowlisted_never_touches_registry() -> None:
    files = _build(repository="oci://evil.example.com/x/y")
    result = validate.run(_args([_PATH]), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_repository_shape_invalid_is_validation_failure() -> None:
    files = _build(repository="oci://ghcr.io")
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_tag_digest_is_validation_failure() -> None:
    files = _build(tags={"3.28.1": TagEntry(content="not-a-digest", observed="T0")})
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_desc_digest_is_validation_failure() -> None:
    desc = Desc(digest="not-a-digest", title="CMake", description="Build tool")
    files = _build(desc=desc)
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_desc_readme_digest_is_validation_failure() -> None:
    desc = Desc(
        digest="sha256:" + "d" * 64,
        title="CMake",
        description="Build tool",
        readme="not-a-digest",
    )
    files = _build(desc=desc)
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_desc_logo_digest_is_validation_failure() -> None:
    desc = Desc(
        digest="sha256:" + "d" * 64,
        title="CMake",
        description="Build tool",
        logo="not-a-digest",
    )
    files = _build(desc=desc)
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_platform_digest_is_validation_failure_never_reaches_registry() -> None:
    # A CAS object whose platforms[*].digest is not `sha256:<64 hex>`-shaped
    # (e.g. a path-traversal payload) must be rejected by `parse_digest`
    # before it ever reaches `registry.get_manifest` — `_PoisonRegistry`
    # proves the network is never touched.
    platform = OciPlatform(architecture="amd64", os="linux")
    object_bytes = _observation_bytes(
        platforms=(PlatformEntry(platform=platform, digest="sha256:aaaa/../../evil"),)
    )
    tag_digest = _content_digest(object_bytes)
    files = _build(
        tags={"3.28.1": TagEntry(content=tag_digest, observed="T0")},
        extra_files={_cas_path(tag_digest): object_bytes},
    )
    result = validate.run(_args([_PATH]), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_digest_out_of_scope_is_validation_failure() -> None:
    files, _registry = _valid_package()
    registry = FakeRegistry(ownership={_REPOSITORY: "confirmed"})  # no manifests registered
    result = validate.run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_ownership_mismatch_is_validation_failure() -> None:
    files, _registry = _valid_package()
    registry = FakeRegistry(
        manifests={(_REPOSITORY, _PLATFORM_DIGEST): {"schemaVersion": 2}},
        ownership={_REPOSITORY: "mismatch"},
    )
    result = validate.run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.VALIDATION_FAILURE


# --- anomalies (exit 65) -------------------------------------------------


def test_run_dangling_reference_is_anomaly() -> None:
    # A syntactically valid digest with no matching CAS object on disk.
    files = _build(tags={"3.28.1": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.ANOMALY


def test_run_tampered_content_digest_is_anomaly() -> None:
    claimed_digest = "sha256:" + "a" * 64
    files = _build(
        tags={"3.28.1": TagEntry(content=claimed_digest, observed="T0")},
        # Present at the claimed path, but its bytes hash to something else
        # entirely — CAS integrity violation.
        extra_files={_cas_path(claimed_digest): b'{"platforms":[]}'},
    )
    result = validate.run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.ANOMALY


# --- aggregation across files ---------------------------------------------


def test_run_aggregates_multiple_files_worst_exit_code_wins() -> None:
    files, registry = _valid_package()
    bad_path = "p/oven-sh/bun.json"
    files.files[bad_path] = _build(
        path=bad_path, name="ocx.sh/oven-sh/other", repository="oci://ghcr.io/ocx-contrib/bun"
    ).files[bad_path]

    result = validate.run(_args([_PATH, bad_path]), files=files, registry=registry)
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_aggregates_validation_and_anomaly_anomaly_wins(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files, registry = _valid_package()
    anomaly_path = "p/oven-sh/bun.json"
    files.files[anomaly_path] = _build(
        path=anomaly_path,
        name="ocx.sh/oven-sh/bun",
        repository="oci://ghcr.io/ocx-contrib/bun",
        tags={"1.0.0": TagEntry(content="sha256:" + "c" * 64, observed="T0")},
    ).files[anomaly_path]

    result = validate.run(_args([_PATH, anomaly_path]), files=files, registry=registry)
    assert result == ExitCode.ANOMALY
    err = capsys.readouterr().err
    assert f"{_PATH}: OK" in err
    assert f"{anomaly_path}: FAIL (ANOMALY)" in err


# --- add_arguments -----------------------------------------------------


def test_add_arguments_registers_paths_and_offline_flag() -> None:
    parser = argparse.ArgumentParser()
    validate.add_arguments(parser)
    parsed = parser.parse_args(["p/kitware/cmake.json", "--offline"])
    assert parsed.paths == ["p/kitware/cmake.json"]
    assert parsed.offline is True


def test_add_arguments_offline_defaults_to_false() -> None:
    parser = argparse.ArgumentParser()
    validate.add_arguments(parser)
    parsed = parser.parse_args(["p/kitware/cmake.json"])
    assert parsed.offline is False


def test_add_arguments_registers_allow_reserved_namespace_flag() -> None:
    parser = argparse.ArgumentParser()
    validate.add_arguments(parser)
    parsed = parser.parse_args(["p/kitware/cmake.json", "--allow-reserved-namespace"])
    assert parsed.allow_reserved_namespace is True


def test_add_arguments_allow_reserved_namespace_defaults_to_false() -> None:
    parser = argparse.ArgumentParser()
    validate.add_arguments(parser)
    parsed = parser.parse_args(["p/kitware/cmake.json"])
    assert parsed.allow_reserved_namespace is False


# --- --allow-reserved-namespace (mechanism only; policy PR-gated) ---------


def test_run_default_still_blocks_brand_segment() -> None:
    path = "p/ocx/cli.json"
    files = _build(path=path, name="ocx.sh/ocx/cli")
    result = validate.run(_args([path]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_allow_reserved_namespace_admits_brand_segment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = "p/ocx/cli.json"
    files = _build(path=path, name="ocx.sh/ocx/cli")
    registry = FakeRegistry(ownership={_REPOSITORY: "confirmed"})
    result = validate.run(
        _args([path], allow_reserved_namespace=True), files=files, registry=registry
    )
    assert result == ExitCode.OK
    assert f"{path}: --allow-reserved-namespace used" in capsys.readouterr().err


def test_run_allow_reserved_namespace_does_not_admit_control_path_segment() -> None:
    path = "p/admin/cmake.json"
    files = _build(path=path, name="ocx.sh/admin/cmake")
    result = validate.run(
        _args([path], allow_reserved_namespace=True), files=files, registry=FakeRegistry()
    )
    assert result == ExitCode.VALIDATION_FAILURE
