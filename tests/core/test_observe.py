from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest

from indexbot.core.observe import observe, observe_one_tag
from indexbot.errors import TransientError
from indexbot.model import ManifestFetch, OwnershipProbeResult
from tests.fakes import FakeRegistry

_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_DIGEST_1 = "sha256:" + "1" * 64
_DIGEST_2 = "sha256:" + "2" * 64
_DIGEST_3 = "sha256:" + "3" * 64
_DIGEST_9 = "sha256:" + "9" * 64


@dataclass
class _RaisingRegistry:
    """Minimal standalone `RegistryPort` double — not `FakeRegistry` (which
    has no configurable way to raise `TransientError` from `list_tags`, and
    fakes are consume-only, never edited)."""

    def list_tags(self, repository: str) -> list[str]:
        raise TransientError("registry unavailable")

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        raise AssertionError("should not be called")

    def get_desc_tag_digest(self, repository: str) -> str | None:
        raise AssertionError("should not be called")

    def get_blob(self, repository: str, digest: str) -> bytes:
        raise AssertionError("should not be called")

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        raise AssertionError("should not be called")


def test_observe_multi_platform_index_sorts_platforms() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={
            (_REPO, "3.28.1"): {
                "manifests": [
                    {"platform": {"architecture": "arm64", "os": "linux"}, "digest": _DIGEST_2},
                    {"platform": {"architecture": "amd64", "os": "linux"}, "digest": _DIGEST_1},
                ]
            }
        },
    )
    result = observe(_REPO, registry)
    assert len(result) == 1
    observation = result[0]
    assert observation.tag == "3.28.1"
    assert [p.platform.architecture for p in observation.object.platforms] == ["amd64", "arm64"]
    assert observation.content_digest.startswith("sha256:")


def test_observe_dual_libc_platforms_sort_stably_by_os_features() -> None:
    # Regression: two platforms sharing architecture/os/os_version/variant
    # and differing ONLY in os.features (the dual-libc glibc/musl case) must
    # not tie under the sort key — a tie would leak the registry's
    # manifest-list order into content_digest (ADR-1 D4).
    manifest_glibc_first: dict[str, object] = {
        "manifests": [
            {
                "platform": {"architecture": "amd64", "os": "linux", "os.features": ["libc.glibc"]},
                "digest": _DIGEST_1,
            },
            {
                "platform": {"architecture": "amd64", "os": "linux", "os.features": ["libc.musl"]},
                "digest": _DIGEST_2,
            },
        ]
    }
    manifest_musl_first: dict[str, object] = {
        "manifests": list(reversed(manifest_glibc_first["manifests"]))  # type: ignore[arg-type]
    }
    registry_a = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): manifest_glibc_first}
    )
    registry_b = FakeRegistry(
        tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): manifest_musl_first}
    )
    result_a = observe(_REPO, registry_a)
    result_b = observe(_REPO, registry_b)
    assert result_a[0].content_digest == result_b[0].content_digest
    assert [p.platform.os_features for p in result_a[0].object.platforms] == [
        ("libc.glibc",),
        ("libc.musl",),
    ]


def test_observe_full_fields_platform_parses_all_fields() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={
            (_REPO, "3.28.1"): {
                "manifests": [
                    {
                        "platform": {
                            "architecture": "arm",
                            "os": "linux",
                            "os.version": "5.15.0",
                            "os.features": ["headless"],
                            "variant": "v7",
                            "features": ["sse4"],
                        },
                        "digest": _DIGEST_3,
                    }
                ]
            }
        },
    )
    result = observe(_REPO, registry)
    platform = result[0].object.platforms[0].platform
    assert platform.os_version == "5.15.0"
    assert platform.os_features == ("headless",)
    assert platform.variant == "v7"
    assert platform.features == ("sse4",)


def test_observe_dedups_identical_platform_sets_across_tags() -> None:
    manifest: dict[str, object] = {
        "manifests": [
            {"platform": {"architecture": "amd64", "os": "linux"}, "digest": _DIGEST_1},
        ]
    }
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1", "latest"]},
        manifests={(_REPO, "3.28.1"): manifest, (_REPO, "latest"): manifest},
    )
    result = observe(_REPO, registry)
    digests = {observation.content_digest for observation in result}
    assert len(digests) == 1


def test_observe_bare_manifest_own_platform_field() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={(_REPO, "3.28.1"): {"platform": {"architecture": "amd64", "os": "linux"}}},
    )
    result = observe(_REPO, registry)
    assert result[0].object.platforms[0].platform.architecture == "amd64"


def test_observe_bare_manifest_config_platform_fallback() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={
            (_REPO, "3.28.1"): {
                "config": {"platform": {"architecture": "arm64", "os": "linux"}},
            }
        },
    )
    result = observe(_REPO, registry)
    assert result[0].object.platforms[0].platform.architecture == "arm64"


def test_observe_bare_manifest_missing_platform_raises() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={(_REPO, "3.28.1"): {"config": {}}},
    )
    with pytest.raises(ValueError, match="platform"):
        observe(_REPO, registry)


def test_observe_bare_manifest_platform_digest_is_registry_content_digest() -> None:
    """A bare manifest's one `PlatformEntry.digest` is
    `RegistryPort.get_manifest`'s adapter-computed `ManifestFetch.digest`
    (ADR-1 D5's verifiability chain) — never a value embedded in the
    manifest's own JSON body, and never a locally synthesized stand-in.
    """
    manifest: dict[str, object] = {"platform": {"architecture": "amd64", "os": "linux"}}
    registry = FakeRegistry(tags={_REPO: ["3.28.1"]}, manifests={(_REPO, "3.28.1"): manifest})
    result = observe(_REPO, registry)
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    expected_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert result[0].object.platforms[0].digest == expected_digest


def test_observe_bare_manifest_digest_ignores_a_digest_key_inside_the_body() -> None:
    # A "digest" field embedded in the manifest's own JSON is registry
    # content like any other — it must never be treated as the platform's
    # real digest (that would defeat the verifiability chain: content could
    # claim any digest for itself).
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={
            (_REPO, "3.28.1"): {
                "platform": {"architecture": "amd64", "os": "linux"},
                "digest": _DIGEST_9,
            }
        },
    )
    result = observe(_REPO, registry)
    assert result[0].object.platforms[0].digest != _DIGEST_9


def test_observe_skips_vanished_tag() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["ghost", "3.28.1"]},
        manifests={(_REPO, "3.28.1"): {"platform": {"architecture": "amd64", "os": "linux"}}},
    )
    # "ghost" has no configured manifest -> FakeRegistry.get_manifest raises KeyError.
    result = observe(_REPO, registry)
    assert [observation.tag for observation in result] == ["3.28.1"]


def test_observe_excludes_internal_desc_tag() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["__ocx.desc", "3.28.1"]},
        manifests={(_REPO, "3.28.1"): {"platform": {"architecture": "amd64", "os": "linux"}}},
    )
    result = observe(_REPO, registry)
    assert [observation.tag for observation in result] == ["3.28.1"]


def test_observe_propagates_transient_error_uncaught() -> None:
    with pytest.raises(TransientError):
        observe(_REPO, _RaisingRegistry())


def test_observe_empty_tag_list_returns_empty_tuple() -> None:
    registry = FakeRegistry(tags={_REPO: []})
    assert observe(_REPO, registry) == ()


# --- observe_one_tag (extracted for core/verify_claims.py + cli/announce.py) -


def test_observe_one_tag_returns_the_single_tag_observation() -> None:
    registry = FakeRegistry(
        manifests={(_REPO, "3.28.1"): {"platform": {"architecture": "amd64", "os": "linux"}}}
    )
    observation = observe_one_tag(_REPO, "3.28.1", registry)
    assert observation is not None
    assert observation.tag == "3.28.1"
    assert observation.object.platforms[0].platform.architecture == "amd64"


def test_observe_one_tag_returns_none_for_a_missing_tag() -> None:
    registry = FakeRegistry()
    assert observe_one_tag(_REPO, "ghost", registry) is None


def test_observe_one_tag_propagates_transient_error_uncaught() -> None:
    @dataclass
    class _RaisingOnGetManifest:
        def list_tags(self, repository: str) -> list[str]:
            raise AssertionError("should not be called")

        def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
            raise TransientError("registry unavailable")

        def get_desc_tag_digest(self, repository: str) -> str | None:
            raise AssertionError("should not be called")

        def get_blob(self, repository: str, digest: str) -> bytes:
            raise AssertionError("should not be called")

        def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
            raise AssertionError("should not be called")

    with pytest.raises(TransientError):
        observe_one_tag(_REPO, "3.28.1", _RaisingOnGetManifest())


# --- org.opencontainers.image.source annotation (Observation.source) --------


def _observe_with_annotations(annotations: object) -> str | None:
    manifest: dict[str, object] = {
        "platform": {"architecture": "amd64", "os": "linux"},
        "annotations": annotations,
    }
    registry = FakeRegistry(manifests={(_REPO, "3.28.1"): manifest})
    observation = observe_one_tag(_REPO, "3.28.1", registry)
    assert observation is not None
    return observation.source


def test_observe_one_tag_reads_https_source_annotation() -> None:
    source = "https://github.com/ocx-sh/mirror-cmake"
    assert _observe_with_annotations({"org.opencontainers.image.source": source}) == source


def test_observe_one_tag_drops_non_https_source_annotation() -> None:
    # The publisher controls this annotation and the value lands as an href
    # on a public page — a javascript:/data:/http: value is dropped at
    # ingestion, never written into a root (where schema/root.schema.json's
    # `source` pattern would reject it at CI time anyway).
    for hostile in ("javascript:alert(1)", "data:text/html,<script>", "http://insecure.test"):
        assert _observe_with_annotations({"org.opencontainers.image.source": hostile}) is None


def test_observe_one_tag_source_none_for_non_string_or_missing_annotation() -> None:
    assert _observe_with_annotations({"org.opencontainers.image.source": 42}) is None
    assert _observe_with_annotations({"other": "https://example.test"}) is None
    assert _observe_with_annotations("not-an-object") is None


def test_observe_one_tag_source_none_when_manifest_has_no_annotations() -> None:
    registry = FakeRegistry(
        manifests={(_REPO, "3.28.1"): {"platform": {"architecture": "amd64", "os": "linux"}}}
    )
    observation = observe_one_tag(_REPO, "3.28.1", registry)
    assert observation is not None
    assert observation.source is None


def test_observe_one_tag_source_annotation_does_not_change_content_digest() -> None:
    # `source` rides on `Observation`, never inside the content-addressed
    # `ObservationObject` — re-annotating an image index must not churn CAS.
    # An *index* manifest, deliberately: a bare manifest's platform digest is
    # the manifest's own content digest, which any body edit moves by
    # definition (see the bare-manifest digest test above).
    plain: dict[str, object] = {
        "manifests": [{"platform": {"architecture": "amd64", "os": "linux"}, "digest": _DIGEST_1}]
    }
    annotated: dict[str, object] = {
        "manifests": [{"platform": {"architecture": "amd64", "os": "linux"}, "digest": _DIGEST_1}],
        "annotations": {"org.opencontainers.image.source": "https://github.com/ocx-sh/index"},
    }
    plain_obs = observe_one_tag(_REPO, "3.28.1", FakeRegistry(manifests={(_REPO, "3.28.1"): plain}))
    annotated_obs = observe_one_tag(
        _REPO, "3.28.1", FakeRegistry(manifests={(_REPO, "3.28.1"): annotated})
    )
    assert plain_obs is not None
    assert annotated_obs is not None
    assert plain_obs.object == annotated_obs.object
    assert plain_obs.content_digest == annotated_obs.content_digest
