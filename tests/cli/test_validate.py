from __future__ import annotations

import argparse
import hashlib
import json

import pytest

from indexbot.cli import validate
from indexbot.core.observe import observe_one_tag
from indexbot.core.validate_entry import serialize_package_root
from indexbot.exit_codes import ExitCode
from indexbot.model import (
    Desc,
    ManifestFetch,
    Owner,
    OwnershipProbeResult,
    PackageRoot,
    TagEntry,
    Upstream,
    Yank,
)
from indexbot.ports import FilePort, RegistryPort
from tests.fakes import FakeRegistry, InMemoryFiles

_NAMESPACE = "kitware"
_PACKAGE = "cmake"
_PATH = f"p/{_NAMESPACE}/{_PACKAGE}.json"
_REPOSITORY = "oci://ghcr.io/ocx-contrib/cmake"
_NAME = f"ocx.sh/{_NAMESPACE}/{_PACKAGE}"
_PLATFORM_DIGEST = "sha256:" + "1" * 64


_ALLOWED_HOSTS = frozenset({"ghcr.io"})


def _run(args: argparse.Namespace, *, files: FilePort, registry: RegistryPort) -> ExitCode:
    """`validate.run` bound to the shipped `{"ghcr.io"}` registry-host policy
    (`.github/index-policy.json`) — every test in this file runs under the
    public index's own allowlist. Tests that need a different policy call
    `validate.run` directly with their own `allowed_hosts`."""
    return validate.run(args, files=files, registry=registry, allowed_hosts=_ALLOWED_HOSTS)


def _cas_path(digest: str, *, ext: str = "json") -> str:
    return f"p/{_NAMESPACE}/{_PACKAGE}/o/sha256/{digest.removeprefix('sha256:')}.{ext}"


def _index(
    *, platform_digest: str = _PLATFORM_DIGEST, architecture: str = "amd64"
) -> dict[str, object]:
    """The only manifest shape this index records — an OCI image index."""
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"platform": {"architecture": architecture, "os": "linux"}, "digest": platform_digest}
        ],
    }


def _content_digest(object_bytes: bytes) -> str:
    return f"sha256:{hashlib.sha256(object_bytes).hexdigest()}"


def _observed_tag(tag: str = "3.28.1") -> tuple[TagEntry, bytes, FakeRegistry]:
    """A committed `TagEntry` + the registry's own index bytes + a
    `FakeRegistry` that still serves them — the online happy-path baseline
    every non-offline test needs, since `core/verify_claims.py` re-observes
    each claimed tag. Also registers a manifest at the index's own descriptor
    digest, so the G-15 digest-in-scope loop passes too."""
    registry = FakeRegistry(tags={_REPOSITORY: [tag]}, ownership={_REPOSITORY: "confirmed"})
    registry.manifests[(_REPOSITORY, tag)] = _index()
    registry.manifests[(_REPOSITORY, _PLATFORM_DIGEST)] = {"schemaVersion": 2}
    observation = observe_one_tag(_REPOSITORY, tag, registry)
    assert observation is not None
    entry = TagEntry(content=observation.content_digest, observed="2026-07-17T00:00:00Z")
    return entry, observation.raw, registry


def _build(
    *,
    path: str = _PATH,
    name: str = _NAME,
    repository: str = _REPOSITORY,
    tags: dict[str, TagEntry] | None = None,
    desc: Desc | None = None,
    upstream: Upstream | None = None,
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
        upstream=upstream,
        tags={} if tags is None else tags,
    )
    files: dict[str, bytes] = {path: serialize_package_root(root)}
    if extra_files:
        files.update(extra_files)
    return InMemoryFiles(files=files)


def _valid_package() -> tuple[InMemoryFiles, FakeRegistry]:
    """One tag, one platform, everything self-consistent and in scope — the
    baseline every online happy-path test starts from."""
    entry, object_bytes, registry = _observed_tag()
    files = _build(tags={"3.28.1": entry}, extra_files={_cas_path(entry.content): object_bytes})
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
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK
    assert f"{_PATH}: OK" in capsys.readouterr().err


def test_run_offline_skips_registry_checks_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    files, _registry = _valid_package()
    result = _run(_args([_PATH], offline=True), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.OK
    err = capsys.readouterr().err
    assert f"{_PATH}: WARN - G-15 registry checks skipped (--offline)" in err


def test_run_no_tags_online_passes_and_still_probes_ownership() -> None:
    files = _build(tags={})
    registry = FakeRegistry(ownership={_REPOSITORY: "confirmed"})
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK


def test_run_tag_with_an_empty_index_passes() -> None:
    registry = FakeRegistry(tags={_REPOSITORY: ["latest"]}, ownership={_REPOSITORY: "confirmed"})
    registry.manifests[(_REPOSITORY, "latest")] = {"manifests": []}
    observation = observe_one_tag(_REPOSITORY, "latest", registry)
    assert observation is not None
    object_bytes = observation.raw
    files = _build(
        tags={
            "latest": TagEntry(content=observation.content_digest, observed="2026-07-17T00:00:00Z")
        },
        extra_files={_cas_path(observation.content_digest): object_bytes},
    )
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK


def test_run_desc_without_readme_or_logo_passes() -> None:
    entry, object_bytes, registry = _observed_tag()
    desc = Desc(digest="sha256:" + "d" * 64, title="CMake", description="Build tool")
    files = _build(
        tags={"3.28.1": entry},
        desc=desc,
        extra_files={_cas_path(entry.content): object_bytes},
    )
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK


def test_run_desc_with_readme_and_logo_passes() -> None:
    entry, object_bytes, registry = _observed_tag()
    readme_bytes = b"# CMake"
    logo_bytes = b"<svg></svg>"
    readme_digest = f"sha256:{hashlib.sha256(readme_bytes).hexdigest()}"
    logo_digest = f"sha256:{hashlib.sha256(logo_bytes).hexdigest()}"
    desc = Desc(
        digest="sha256:" + "d" * 64,
        title="CMake",
        description="Build tool",
        readme=readme_digest,
        logo=logo_digest,
    )
    files = _build(
        tags={"3.28.1": entry},
        desc=desc,
        extra_files={
            _cas_path(entry.content): object_bytes,
            _cas_path(readme_digest, ext="md"): readme_bytes,
            _cas_path(logo_digest, ext="svg"): logo_bytes,
        },
    )
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK


def test_run_desc_readme_hash_mismatch_is_anomaly() -> None:
    # Byte-exact discipline (fork-PR announce revamp): desc blobs are now
    # hash-checked too, not just presence-checked.
    entry, object_bytes, registry = _observed_tag()
    readme_digest = "sha256:" + "e" * 64
    desc = Desc(digest="sha256:" + "d" * 64, title="CMake", description="x", readme=readme_digest)
    files = _build(
        tags={"3.28.1": entry},
        desc=desc,
        extra_files={
            _cas_path(entry.content): object_bytes,
            _cas_path(readme_digest, ext="md"): b"not the readme bytes",
        },
    )
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.ANOMALY


def test_run_ownership_unconfirmed_warns_but_passes(capsys: pytest.CaptureFixture[str]) -> None:
    files, registry = _valid_package()
    del registry.ownership[_REPOSITORY]
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK
    assert "WARN - ownership unconfirmed (G-15)" in capsys.readouterr().err


# --- validation failures (exit 1) ---------------------------------------


def test_run_missing_file_is_validation_failure() -> None:
    files = InMemoryFiles(files={})
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_json_is_validation_failure() -> None:
    files = InMemoryFiles(files={_PATH: b"not json"})
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_non_canonical_root_bytes_is_validation_failure() -> None:
    # Byte-exact discipline (fork-PR announce revamp): the same JSON content,
    # differently formatted (minified, not `serialize_package_root`'s
    # pretty-printed canonical form), must be rejected even though it parses
    # to a structurally valid root.
    root_bytes = _build(tags={}).files[_PATH]
    minified = json.dumps(json.loads(root_bytes), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    files = InMemoryFiles(files={_PATH: minified})
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_bad_path_shape_is_validation_failure() -> None:
    bad_path = "p/kitware.json"
    files = InMemoryFiles(files={bad_path: _build().files[_PATH]})
    result = _run(_args([bad_path]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_name_path_mismatch_is_validation_failure() -> None:
    files = _build(name="ocx.sh/kitware/other-tool")
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


# --- upstream.repository_url scheme (review-round-1 finding #1) -----------


def test_run_upstream_javascript_scheme_never_touches_registry() -> None:
    upstream = Upstream(org="Evil", repository_url="javascript:alert(1)")
    files = _build(upstream=upstream)
    result = _run(_args([_PATH]), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_upstream_https_scheme_passes(capsys: pytest.CaptureFixture[str]) -> None:
    upstream = Upstream(org="Kitware", repository_url="https://github.com/Kitware/CMake")
    files = _build(tags={}, upstream=upstream)
    registry = FakeRegistry(ownership={_REPOSITORY: "confirmed"})
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.OK
    assert f"{_PATH}: OK" in capsys.readouterr().err


# --- tag timestamp Z-anchoring (review-round-1 finding #3) -----------------


def test_run_tag_observed_with_utc_offset_is_validation_failure() -> None:
    files = _build(
        tags={
            "3.28.1": TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00+02:00")
        }
    )
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_tag_yanked_at_with_utc_offset_is_validation_failure() -> None:
    yank = Yank(reason="cve", at="2026-07-17T00:00:00+02:00")
    files = _build(
        tags={
            "3.28.1": TagEntry(
                content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z", yanked=yank
            )
        }
    )
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_reserved_namespace_is_validation_failure() -> None:
    path = "p/admin/cmake.json"
    files = _build(path=path, name="ocx.sh/admin/cmake")
    result = _run(_args([path]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_repository_not_allowlisted_never_touches_registry() -> None:
    files = _build(repository="oci://evil.example.com/x/y")
    result = _run(_args([_PATH]), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_repository_shape_invalid_is_validation_failure() -> None:
    files = _build(repository="oci://ghcr.io")
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_tag_digest_is_validation_failure() -> None:
    files = _build(
        tags={"3.28.1": TagEntry(content="not-a-digest", observed="2026-07-17T00:00:00Z")}
    )
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_desc_digest_is_validation_failure() -> None:
    desc = Desc(digest="not-a-digest", title="CMake", description="Build tool")
    files = _build(desc=desc)
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_desc_readme_digest_is_validation_failure() -> None:
    desc = Desc(
        digest="sha256:" + "d" * 64,
        title="CMake",
        description="Build tool",
        readme="not-a-digest",
    )
    files = _build(desc=desc)
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_desc_logo_digest_is_validation_failure() -> None:
    desc = Desc(
        digest="sha256:" + "d" * 64,
        title="CMake",
        description="Build tool",
        logo="not-a-digest",
    )
    files = _build(desc=desc)
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_malformed_platform_digest_is_validation_failure_never_reaches_registry() -> None:
    # A CAS object whose manifests[*].digest is not `sha256:<64 hex>`-shaped
    # (e.g. a path-traversal payload) must be rejected by `parse_digest`
    # before it ever reaches `registry.get_manifest` — `_PoisonRegistry`
    # proves the network is never touched.
    object_bytes = json.dumps(_index(platform_digest="sha256:aaaa/../../evil")).encode("utf-8")
    tag_digest = _content_digest(object_bytes)
    files = _build(
        tags={"3.28.1": TagEntry(content=tag_digest, observed="2026-07-17T00:00:00Z")},
        extra_files={_cas_path(tag_digest): object_bytes},
    )
    result = _run(_args([_PATH]), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_digest_out_of_scope_is_validation_failure() -> None:
    files, _registry = _valid_package()
    registry = FakeRegistry(ownership={_REPOSITORY: "confirmed"})  # no manifests registered
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_ownership_mismatch_is_validation_failure() -> None:
    files, registry = _valid_package()
    registry.ownership[_REPOSITORY] = "mismatch"
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_claim_digest_mismatch_is_validation_failure() -> None:
    # Registry drift *after* the root was committed: the tag now resolves to
    # a different manifest, so the claim no longer matches registry truth,
    # even though the committed CAS object is still internally
    # self-consistent and its platform digest is still in scope.
    entry, object_bytes, registry = _observed_tag()
    files = _build(tags={"3.28.1": entry}, extra_files={_cas_path(entry.content): object_bytes})
    registry.manifests[(_REPOSITORY, "3.28.1")] = _index(architecture="arm64")
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_claim_retagged_to_a_bare_manifest_reports_a_finding(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A tag re-pointed at a single image manifest reaches the operator as a
    `digest-mismatch` *finding* from `verify_claims`, not as an escaped
    `observe_one_tag` shape error. Both land on exit 1 here, so the exit code
    alone cannot tell them apart — the message is what proves the claim was
    verified rather than the sweep aborted."""
    entry, object_bytes, registry = _observed_tag()
    files = _build(tags={"3.28.1": entry}, extra_files={_cas_path(entry.content): object_bytes})
    registry.manifests[(_REPOSITORY, "3.28.1")] = {"config": {"digest": "sha256:" + "9" * 64}}
    result = _run(_args([_PATH]), files=files, registry=registry)
    assert result == ExitCode.VALIDATION_FAILURE
    err = capsys.readouterr().err
    assert "claim verification failed" in err
    assert "digest-mismatch: 3.28.1" in err


# --- D7: a hand-authored root may not carry a reserved tag ----------------


@pytest.mark.parametrize(
    "tag",
    [
        "__ocx.desc",
        "__ocx",
        "__ocxfoo",
        "__OCX.desc",
        "sha256." + "a" * 64,
        "sha384." + "b" * 96,
        "sha512." + "c" * 128,
    ],
)
def test_run_reserved_tag_is_validation_failure_and_never_touches_registry(tag: str) -> None:
    """The PR gate is the only layer a hand-authored root passes through, so
    the reserved-tag rejection lives here — and it lands before any network
    call, which `_PoisonRegistry` proves."""
    files = _build(
        tags={tag: TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z")}
    )
    result = _run(_args([_PATH]), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_ordinary_tag_names_still_pass() -> None:
    entry, object_bytes, registry = _observed_tag()
    files = _build(tags={"3.28.1": entry}, extra_files={_cas_path(entry.content): object_bytes})
    assert _run(_args([_PATH]), files=files, registry=registry) == ExitCode.OK


# --- D4(c): a committed CAS object must be an OCI image index -------------


def test_run_cas_object_that_is_not_an_image_index_is_validation_failure() -> None:
    """The bytes hash correctly to their own filename — only the document-kind
    check catches that they are a bare image manifest, not an index."""
    object_bytes = json.dumps({"schemaVersion": 2, "config": {"digest": _PLATFORM_DIGEST}}).encode(
        "utf-8"
    )
    tag_digest = _content_digest(object_bytes)
    files = _build(
        tags={"3.28.1": TagEntry(content=tag_digest, observed="2026-07-17T00:00:00Z")},
        extra_files={_cas_path(tag_digest): object_bytes},
    )
    result = _run(_args([_PATH]), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_cas_object_kind_is_checked_even_offline() -> None:
    """A governance check a flag can switch off is not a governance check."""
    object_bytes = b'{"schemaVersion":2,"layers":[]}'
    tag_digest = _content_digest(object_bytes)
    files = _build(
        tags={"3.28.1": TagEntry(content=tag_digest, observed="2026-07-17T00:00:00Z")},
        extra_files={_cas_path(tag_digest): object_bytes},
    )
    result = _run(_args([_PATH], offline=True), files=files, registry=_PoisonRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


# --- anomalies (exit 65) -------------------------------------------------


def test_run_dangling_reference_is_anomaly() -> None:
    # A syntactically valid digest with no matching CAS object on disk.
    files = _build(
        tags={"3.28.1": TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z")}
    )
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.ANOMALY


def test_run_tampered_content_digest_is_anomaly() -> None:
    claimed_digest = "sha256:" + "a" * 64
    files = _build(
        tags={"3.28.1": TagEntry(content=claimed_digest, observed="2026-07-17T00:00:00Z")},
        # Present at the claimed path, but its bytes hash to something else
        # entirely — CAS integrity violation.
        extra_files={_cas_path(claimed_digest): b'{"manifests":[]}'},
    )
    result = _run(_args([_PATH]), files=files, registry=FakeRegistry())
    assert result == ExitCode.ANOMALY


# --- aggregation across files ---------------------------------------------


def test_run_aggregates_multiple_files_worst_exit_code_wins() -> None:
    files, registry = _valid_package()
    bad_path = "p/oven-sh/bun.json"
    files.files[bad_path] = _build(
        path=bad_path, name="ocx.sh/oven-sh/other", repository="oci://ghcr.io/ocx-contrib/bun"
    ).files[bad_path]

    result = _run(_args([_PATH, bad_path]), files=files, registry=registry)
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
        tags={"1.0.0": TagEntry(content="sha256:" + "c" * 64, observed="2026-07-17T00:00:00Z")},
    ).files[anomaly_path]

    result = _run(_args([_PATH, anomaly_path]), files=files, registry=registry)
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
    result = _run(_args([path]), files=files, registry=FakeRegistry())
    assert result == ExitCode.VALIDATION_FAILURE


def test_run_allow_reserved_namespace_admits_brand_segment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = "p/ocx/cli.json"
    files = _build(path=path, name="ocx.sh/ocx/cli")
    registry = FakeRegistry(ownership={_REPOSITORY: "confirmed"})
    result = _run(_args([path], allow_reserved_namespace=True), files=files, registry=registry)
    assert result == ExitCode.OK
    assert f"{path}: --allow-reserved-namespace used" in capsys.readouterr().err


def test_run_allow_reserved_namespace_does_not_admit_control_path_segment() -> None:
    path = "p/admin/cmake.json"
    files = _build(path=path, name="ocx.sh/admin/cmake")
    result = _run(
        _args([path], allow_reserved_namespace=True), files=files, registry=FakeRegistry()
    )
    assert result == ExitCode.VALIDATION_FAILURE


# --- ND-4 gates CLAIMING, not UPDATING (the fork re-announce lane) ---------

_RESERVED_PATH = "p/ocx/cli.json"
_RESERVED_NAME = "ocx.sh/ocx/cli"
_RESERVED_INDEX_BYTES = json.dumps(_index()).encode("utf-8")
_RESERVED_DIGEST = _content_digest(_RESERVED_INDEX_BYTES)
_RESERVED_CAS = {
    f"p/ocx/cli/o/sha256/{_RESERVED_DIGEST.removeprefix('sha256:')}.json": _RESERVED_INDEX_BYTES
}


def _reserved_root(
    *,
    repository: str = _REPOSITORY,
    owners: tuple[Owner, ...] = (Owner(github="alice", github_id=1),),
    status: str = "active",
    tags: dict[str, TagEntry] | None = None,
) -> PackageRoot:
    """A first-party root under the reserved `ocx` brand segment — the shape
    `p/ocx/cli.json` actually has on `main`."""
    return PackageRoot(
        name=_RESERVED_NAME,
        repository=repository,
        owners=owners,
        status=status,  # type: ignore[arg-type]
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        upstream=None,
        tags={} if tags is None else tags,
    )


def _fork_validate(head: PackageRoot, base: PackageRoot | None) -> ExitCode:
    """`indexbot validate` exactly as the FORK half of the PR gate invokes it:
    no `--allow-reserved-namespace` (withheld for a head repo that is not this
    repo), plus the base-ref bytes the workflow materializes into
    `--base-dir`. `base=None` models a path that does not exist at the base
    ref at all — a new claim."""
    return validate.run(
        _args([_RESERVED_PATH], offline=True),
        files=InMemoryFiles(files={_RESERVED_PATH: serialize_package_root(head), **_RESERVED_CAS}),
        registry=_PoisonRegistry(),
        allowed_hosts=_ALLOWED_HOSTS,
        base_files=InMemoryFiles(
            files={} if base is None else {_RESERVED_PATH: serialize_package_root(base)}
        ),
    )


def test_add_arguments_base_dir_defaults_to_none() -> None:
    parser = argparse.ArgumentParser()
    validate.add_arguments(parser)
    assert parser.parse_args(["p/kitware/cmake.json"]).base_dir is None


def test_add_arguments_registers_base_dir() -> None:
    parser = argparse.ArgumentParser()
    validate.add_arguments(parser)
    parsed = parser.parse_args(["p/kitware/cmake.json", "--base-dir", "base-ref"])
    assert parsed.base_dir == "base-ref"


def test_fork_pr_may_refresh_a_reserved_root_already_on_the_base_ref(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The lane this exemption exists for: `ocx package announce --fork` can
    open nothing but fork PRs, and a fork PR never gets
    `--allow-reserved-namespace`. Moving `tags` on `p/ocx/cli.json`, which is
    already committed, is not a claim."""
    base = _reserved_root()
    head = _reserved_root(
        tags={"1.0.0": TagEntry(content=_RESERVED_DIGEST, observed="2026-07-17T00:00:00Z")}
    )
    assert _fork_validate(head, base) == ExitCode.OK
    assert "reserved segment admitted" in capsys.readouterr().err


def test_fork_pr_still_cannot_claim_a_new_reserved_root() -> None:
    """The control ND-4 actually exists for: no such root at the base ref
    means this PR is claiming the brand segment, not updating it."""
    assert _fork_validate(_reserved_root(), None) == ExitCode.VALIDATION_FAILURE


def test_fork_pr_cannot_repoint_an_existing_reserved_root() -> None:
    """The sharpest scoping case: `repository` redirects every future pull,
    so repointing it is not announce-shaped and stays rejected by this
    REQUIRED check — never merely routed to the human lane behind
    `governance/review-required`, which is not required and cannot block a
    careless merge."""
    base = _reserved_root()
    head = _reserved_root(repository="oci://ghcr.io/attacker/cli")
    assert _fork_validate(head, base) == ExitCode.VALIDATION_FAILURE


def test_fork_pr_cannot_write_itself_into_an_existing_reserved_roots_owners() -> None:
    base = _reserved_root()
    head = _reserved_root(
        owners=(Owner(github="alice", github_id=1), Owner(github="mallory", github_id=999))
    )
    assert _fork_validate(head, base) == ExitCode.VALIDATION_FAILURE


def test_fork_pr_cannot_change_an_existing_reserved_roots_status() -> None:
    base = _reserved_root()
    head = _reserved_root(status="deprecated")
    assert _fork_validate(head, base) == ExitCode.VALIDATION_FAILURE


def test_the_exemption_never_widens_a_control_path_segment() -> None:
    """`p`/`admin`-class segments are not brand segments: no PR provenance and
    no base-ref presence admits them, because the collision they guard is with
    the URL layout itself."""
    path = "p/admin/cmake.json"
    root = PackageRoot(
        name="ocx.sh/admin/cmake",
        repository=_REPOSITORY,
        owners=(Owner(github="alice", github_id=1),),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        upstream=None,
        tags={},
    )
    serialized = serialize_package_root(root)
    result = validate.run(
        _args([path], offline=True),
        files=InMemoryFiles(files={path: serialized}),
        registry=_PoisonRegistry(),
        allowed_hosts=_ALLOWED_HOSTS,
        base_files=InMemoryFiles(files={path: serialized}),
    )
    assert result == ExitCode.VALIDATION_FAILURE
