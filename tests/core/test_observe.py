from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest

from indexbot.core.observe import observe, observe_one_tag
from indexbot.errors import TransientError, ValidationError
from indexbot.model import ManifestFetch, OwnershipProbeResult
from tests.fakes import FakeRegistry

_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_DIGEST_1 = "sha256:" + "1" * 64
_DIGEST_2 = "sha256:" + "2" * 64


def _index(*platforms: dict[str, str], digest: str = _DIGEST_1) -> dict[str, object]:
    """A minimal OCI image index — the only manifest shape this index records."""
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"platform": platform, "digest": digest} for platform in (platforms or ({},))
        ],
    }


@dataclass
class _FixedFetchRegistry:
    """Serves one caller-built `ManifestFetch`, the *same object* every call.

    `FakeRegistry` re-encodes on every `get_manifest`, so no test using it can
    tell "copied the registry's bytes through" from "re-encoded them into an
    equal-looking value" — and that distinction is the whole contract (§1).
    This double makes it observable, and lets a test hand over bytes that are
    *not* in any canonical form, so a re-encoder would be caught by equality
    too."""

    fetch: ManifestFetch

    def list_tags(self, repository: str) -> list[str]:
        raise AssertionError("should not be called")

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        return self.fetch

    def get_desc_tag_digest(self, repository: str) -> str | None:
        raise AssertionError("should not be called")

    def get_blob(self, repository: str, digest: str) -> bytes:
        raise AssertionError("should not be called")

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        raise AssertionError("should not be called")


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


# --- the bytes are the registry's, unmodified ------------------------------


def test_observe_one_tag_stores_raw_registry_bytes() -> None:
    """`Observation.raw` is the exact object `RegistryPort` returned — the
    same `bytes` instance, not an equal-looking re-encoding — and
    `content_digest` is the registry's own digest over those bytes.

    Asserted with `is`, deliberately. `==` would still pass if this module
    grew back a `json.dumps(json.loads(raw), sort_keys=True, ...)` round-trip
    — the exact encoder the ADR deleted. The registry's bytes here are
    pretty-printed with the keys out of sorted order, so the re-encoding this
    pins against would produce visibly different bytes."""
    raw = b'{\n  "schemaVersion": 2,\n  "manifests": [\n    {"digest": "%s"}\n  ]\n}' % (
        _DIGEST_1.encode()
    )
    registry = _FixedFetchRegistry(
        ManifestFetch(
            raw=raw,
            digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
            parsed=json.loads(raw),
        )
    )
    fetch = registry.get_manifest(_REPO, "3.28.1")
    observation = observe_one_tag(_REPO, "3.28.1", registry)
    assert observation is not None
    assert observation.raw is fetch.raw
    assert observation.content_digest == fetch.digest
    assert json.loads(observation.raw)["manifests"][0]["digest"] == _DIGEST_1


def test_observe_one_tag_refuses_a_bare_manifest() -> None:
    """D4(a): a tag resolving to a single image manifest is refused, naming
    both the tag and the repository — there is no stand-in index to
    manufacture."""
    registry = FakeRegistry(
        manifests={(_REPO, "3.28.1"): {"platform": {"architecture": "amd64", "os": "linux"}}}
    )
    with pytest.raises(ValidationError) as excinfo:
        observe_one_tag(_REPO, "3.28.1", registry)
    assert "3.28.1" in str(excinfo.value)
    assert _REPO in str(excinfo.value)


def test_observe_one_tag_refuses_an_oversized_index() -> None:
    """Verbatim storage means the registry decides how many bytes each tag
    commits to this git repository, permanently, and an image index's
    `annotations` are unbounded. A padded index is refused at the one point
    registry bytes enter."""
    padding = "x" * (4 * 1024 * 1024)
    registry = FakeRegistry(
        manifests={(_REPO, "3.28.1"): {"manifests": [], "annotations": {"pad": padding}}}
    )
    with pytest.raises(ValidationError) as excinfo:
        observe_one_tag(_REPO, "3.28.1", registry)
    assert "ceiling" in str(excinfo.value)
    assert "3.28.1" in str(excinfo.value)


def test_observe_one_tag_accepts_an_index_at_the_ceiling() -> None:
    """The bound rejects *over* the ceiling, not at it — a legitimately large
    index must not be refused by an off-by-one."""
    prefix = b'{"manifests":[],"annotations":{"pad":"'
    suffix = b'"}}'
    padding = b"x" * (4 * 1024 * 1024 - len(prefix) - len(suffix))
    raw = prefix + padding + suffix
    assert len(raw) == 4 * 1024 * 1024
    registry = _FixedFetchRegistry(
        ManifestFetch(
            raw=raw,
            digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
            parsed={
                "manifests": [],
            },
        )
    )
    observation = observe_one_tag(_REPO, "3.28.1", registry)
    assert observation is not None
    assert observation.raw is raw


def test_observe_identical_indices_across_tags_share_one_digest() -> None:
    """Byte-identical registry responses dedup to one CAS object (ADR-1 D3)."""
    manifest = _index({"architecture": "amd64", "os": "linux"})
    registry = FakeRegistry(
        tags={_REPO: ["3.28.1", "latest"]},
        manifests={(_REPO, "3.28.1"): manifest, (_REPO, "latest"): manifest},
    )
    result = observe(_REPO, registry)
    assert {observation.content_digest for observation in result} == {result[0].content_digest}
    assert {observation.raw for observation in result} == {result[0].raw}


def test_observe_index_with_no_manifests_is_still_an_index() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["latest"]}, manifests={(_REPO, "latest"): {"manifests": []}}
    )
    result = observe(_REPO, registry)
    assert json.loads(result[0].raw) == {"manifests": []}


# --- the sweep excludes reserved tags, it does not refuse them (ADR R3) ----


def test_observe_excludes_canonical_sha256_dot_tags() -> None:
    """`ocx package push` writes a `sha256.<hex>` tag beside every version
    tag, pointing at a bare manifest. The sweep must skip it — refusing it
    would abort reconcile for every ocx-published repository."""
    canonical = "sha256." + "1" * 64
    registry = FakeRegistry(
        tags={_REPO: ["1.0.0", canonical]},
        manifests={
            (_REPO, "1.0.0"): _index({"architecture": "amd64", "os": "linux"}),
            # A bare manifest, exactly as ocx publishes it under this tag —
            # observing it would raise, so the test proves it is never fetched.
            (_REPO, canonical): {"platform": {"architecture": "amd64", "os": "linux"}},
        },
    )
    assert [observation.tag for observation in observe(_REPO, registry)] == ["1.0.0"]


def test_observe_excludes_every_reserved_tag_form() -> None:
    reserved = ["__ocx.desc", "__ocx", "__ocxfoo", "__OCX.desc", "sha384." + "a" * 96]
    registry = FakeRegistry(
        tags={_REPO: ["1.0.0", *reserved]},
        manifests={(_REPO, "1.0.0"): _index({"architecture": "amd64", "os": "linux"})},
    )
    assert [observation.tag for observation in observe(_REPO, registry)] == ["1.0.0"]


def test_observe_still_excludes___ocx_desc() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["__ocx.desc", "3.28.1"]},
        manifests={(_REPO, "3.28.1"): _index({"architecture": "amd64", "os": "linux"})},
    )
    assert [observation.tag for observation in observe(_REPO, registry)] == ["3.28.1"]


# --- loop behaviour --------------------------------------------------------


def test_observe_skips_vanished_tag() -> None:
    registry = FakeRegistry(
        tags={_REPO: ["ghost", "3.28.1"]},
        manifests={(_REPO, "3.28.1"): _index({"architecture": "amd64", "os": "linux"})},
    )
    # "ghost" has no configured manifest -> FakeRegistry.get_manifest raises KeyError.
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
        manifests={(_REPO, "3.28.1"): _index({"architecture": "amd64", "os": "linux"})}
    )
    observation = observe_one_tag(_REPO, "3.28.1", registry)
    assert observation is not None
    assert observation.tag == "3.28.1"


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
    """An image index (the only shape `observe_one_tag` accepts) carrying
    `annotations` verbatim, whatever they are."""
    manifest: dict[str, object] = {
        "manifests": [{"platform": {"architecture": "amd64", "os": "linux"}, "digest": _DIGEST_2}],
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
        manifests={(_REPO, "3.28.1"): _index({"architecture": "amd64", "os": "linux"})}
    )
    observation = observe_one_tag(_REPO, "3.28.1", registry)
    assert observation is not None
    assert observation.source is None
