"""`core/validate_entry.py` — pure semantic checks + the PackageRoot/
ObservationObject <-> dict codec. DAMP, self-contained (no shared fixture
module beyond `tests/fakes/`, CONTRACTS.md §2).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from indexbot.core import validate_entry
from indexbot.core.registry_checks import check_digest_in_scope
from indexbot.errors import AnomalyError, ValidationError
from indexbot.model import (
    Desc,
    ManifestFetch,
    ObservationObject,
    OciPlatform,
    Owner,
    OwnershipProbeResult,
    PackageId,
    PackageRoot,
    PlatformEntry,
    TagEntry,
    Upstream,
    Yank,
)


def _minimal_root(**overrides: object) -> PackageRoot:
    defaults: dict[str, object] = {
        "name": "ocx.sh/kitware/cmake",
        "repository": "oci://ghcr.io/ocx-contrib/cmake",
        "owners": (Owner(github="alice", github_id=123456),),
        "status": "active",
        "deprecated_message": None,
        "created": "2026-07-17",
        "desc": None,
        "upstream": None,
        "superseded_by": None,
        "tags": {},
    }
    defaults.update(overrides)
    return PackageRoot(**defaults)  # type: ignore[arg-type]


# --- check_name_matches_path -------------------------------------------------


def test_check_name_matches_path_ok() -> None:
    package_id = PackageId(namespace="kitware", package="cmake")
    root = _minimal_root(name="ocx.sh/kitware/cmake")
    validate_entry.check_name_matches_path(package_id, root)  # no raise


def test_check_name_matches_path_mismatch_raises() -> None:
    package_id = PackageId(namespace="kitware", package="cmake")
    root = _minimal_root(name="ocx.sh/astral-sh/uv")
    with pytest.raises(ValidationError, match="G-02"):
        validate_entry.check_name_matches_path(package_id, root)


# --- check_superseded_by -----------------------------------------------------


def test_check_superseded_by_none_is_a_noop() -> None:
    root = _minimal_root(superseded_by=None)
    validate_entry.check_superseded_by(root)  # no raise


def test_check_superseded_by_valid_successor_ok() -> None:
    root = _minimal_root(name="ocx.sh/kitware/cmake", superseded_by="kitware/cmake-ng")
    validate_entry.check_superseded_by(root)  # no raise


def test_check_superseded_by_bad_shape_raises() -> None:
    root = _minimal_root(superseded_by="not-a-valid-id")
    with pytest.raises(ValidationError, match="not a valid"):
        validate_entry.check_superseded_by(root)


def test_check_superseded_by_self_reference_raises() -> None:
    root = _minimal_root(name="ocx.sh/kitware/cmake", superseded_by="kitware/cmake")
    with pytest.raises(ValidationError, match="cannot reference its own package"):
        validate_entry.check_superseded_by(root)


# --- check_upstream_repository_url_scheme (review-round-1 finding #1) -------


def test_check_upstream_repository_url_scheme_upstream_none_is_a_noop() -> None:
    root = _minimal_root(upstream=None)
    validate_entry.check_upstream_repository_url_scheme(root)  # no raise


def test_check_upstream_repository_url_scheme_repository_url_none_is_a_noop() -> None:
    root = _minimal_root(upstream=Upstream(org="Kitware", repository_url=None))
    validate_entry.check_upstream_repository_url_scheme(root)  # no raise


def test_check_upstream_repository_url_scheme_https_ok() -> None:
    root = _minimal_root(
        upstream=Upstream(org="Kitware", repository_url="https://github.com/Kitware/CMake")
    )
    validate_entry.check_upstream_repository_url_scheme(root)  # no raise


def test_check_upstream_repository_url_scheme_http_ok() -> None:
    root = _minimal_root(upstream=Upstream(org="Kitware", repository_url="http://example.com/x"))
    validate_entry.check_upstream_repository_url_scheme(root)  # no raise


def test_check_upstream_repository_url_scheme_javascript_scheme_raises() -> None:
    root = _minimal_root(upstream=Upstream(org="Evil", repository_url="javascript:alert(1)"))
    with pytest.raises(ValidationError, match="http or https"):
        validate_entry.check_upstream_repository_url_scheme(root)


def test_check_upstream_repository_url_scheme_data_scheme_raises() -> None:
    root = _minimal_root(
        upstream=Upstream(org="Evil", repository_url="data:text/html,<script>alert(1)</script>")
    )
    with pytest.raises(ValidationError, match="http or https"):
        validate_entry.check_upstream_repository_url_scheme(root)


def test_check_upstream_repository_url_scheme_no_scheme_raises() -> None:
    root = _minimal_root(
        upstream=Upstream(org="Kitware", repository_url="github.com/Kitware/CMake")
    )
    with pytest.raises(ValidationError, match="http or https"):
        validate_entry.check_upstream_repository_url_scheme(root)


# --- check_tag_timestamps_z_anchored (review-round-1 finding #3) -----------


def test_check_tag_timestamps_z_anchored_no_tags_is_a_noop() -> None:
    root = _minimal_root(tags={})
    validate_entry.check_tag_timestamps_z_anchored(root)  # no raise


def test_check_tag_timestamps_z_anchored_ok() -> None:
    tag = TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z")
    root = _minimal_root(tags={"3.28.1": tag})
    validate_entry.check_tag_timestamps_z_anchored(root)  # no raise


def test_check_tag_timestamps_z_anchored_yanked_at_ok() -> None:
    yank = Yank(reason="cve", at="2026-07-18T00:00:00Z")
    tag = TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z", yanked=yank)
    root = _minimal_root(tags={"3.27.0": tag})
    validate_entry.check_tag_timestamps_z_anchored(root)  # no raise


def test_check_tag_timestamps_z_anchored_observed_offset_raises() -> None:
    tag = TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00+02:00")
    root = _minimal_root(tags={"3.28.1": tag})
    with pytest.raises(ValidationError, match=r"tags\[3\.28\.1\]\.observed"):
        validate_entry.check_tag_timestamps_z_anchored(root)


def test_check_tag_timestamps_z_anchored_yanked_at_offset_raises() -> None:
    yank = Yank(reason="cve", at="2026-07-18T00:00:00+02:00")
    tag = TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z", yanked=yank)
    root = _minimal_root(tags={"3.27.0": tag})
    with pytest.raises(ValidationError, match=r"tags\[3\.27\.0\]\.yanked\.at"):
        validate_entry.check_tag_timestamps_z_anchored(root)


# --- check_namespace_not_reserved -------------------------------------------


def test_check_namespace_not_reserved_ok() -> None:
    validate_entry.check_namespace_not_reserved(PackageId(namespace="kitware", package="cmake"))


def test_check_namespace_not_reserved_namespace_reserved_raises() -> None:
    package_id = PackageId(namespace="ocx-contrib", package="cmake")
    with pytest.raises(ValidationError, match="ADR-2 ND-4"):
        validate_entry.check_namespace_not_reserved(package_id)


def test_check_namespace_not_reserved_package_reserved_raises() -> None:
    with pytest.raises(ValidationError, match="ADR-2 ND-4"):
        validate_entry.check_namespace_not_reserved(PackageId(namespace="kitware", package="admin"))


def test_reserved_segments_cover_control_paths_brand_and_generic() -> None:
    # Locks the ADR-2 ND-4 list against accidental drift.
    assert "p" in validate_entry.RESERVED_NAMESPACE_SEGMENTS
    assert "o" in validate_entry.RESERVED_NAMESPACE_SEGMENTS
    assert "ocx-contrib" in validate_entry.RESERVED_NAMESPACE_SEGMENTS
    assert "admin" in validate_entry.RESERVED_NAMESPACE_SEGMENTS
    assert "kitware" not in validate_entry.RESERVED_NAMESPACE_SEGMENTS


def test_reserved_brand_segments_is_a_subset_of_the_full_reserved_list() -> None:
    assert {"ocx", "ocx-sh", "ocx-contrib", "ocx-rs"} == validate_entry.RESERVED_BRAND_SEGMENTS
    assert validate_entry.RESERVED_BRAND_SEGMENTS <= validate_entry.RESERVED_NAMESPACE_SEGMENTS


def test_check_namespace_not_reserved_allow_reserved_default_false_still_blocks_brand() -> None:
    with pytest.raises(ValidationError, match="ADR-2 ND-4"):
        validate_entry.check_namespace_not_reserved(PackageId(namespace="ocx", package="cli"))


def test_check_namespace_not_reserved_allow_reserved_admits_brand_namespace() -> None:
    package_id = PackageId(namespace="ocx", package="cli")
    validate_entry.check_namespace_not_reserved(package_id, allow_reserved=True)  # no raise


def test_check_namespace_not_reserved_allow_reserved_admits_brand_package() -> None:
    package_id = PackageId(namespace="kitware", package="ocx-rs")
    validate_entry.check_namespace_not_reserved(package_id, allow_reserved=True)  # no raise


def test_check_namespace_not_reserved_allow_reserved_still_blocks_control_path_segment() -> None:
    with pytest.raises(ValidationError, match="ADR-2 ND-4"):
        validate_entry.check_namespace_not_reserved(
            PackageId(namespace="p", package="cmake"), allow_reserved=True
        )


def test_check_namespace_not_reserved_allow_reserved_still_blocks_generic_segment() -> None:
    with pytest.raises(ValidationError, match="ADR-2 ND-4"):
        validate_entry.check_namespace_not_reserved(
            PackageId(namespace="admin", package="cmake"), allow_reserved=True
        )


# --- check_repository_allowlisted (G-03, SSRF ordering) ---------------------


def test_check_repository_allowlisted_ok() -> None:
    validate_entry.check_repository_allowlisted("oci://ghcr.io/ocx-contrib/cmake")


def test_check_repository_allowlisted_uppercase_host_lowercase_folds() -> None:
    validate_entry.check_repository_allowlisted("oci://GHCR.IO/ocx-contrib/cmake")


def test_check_repository_allowlisted_wrong_scheme_raises() -> None:
    with pytest.raises(ValidationError):
        validate_entry.check_repository_allowlisted("https://ghcr.io/ocx-contrib/cmake")


def test_check_repository_allowlisted_no_scheme_at_all_raises() -> None:
    with pytest.raises(ValidationError):
        validate_entry.check_repository_allowlisted("ghcr.io/ocx-contrib/cmake")


def test_check_repository_allowlisted_empty_netloc_raises() -> None:
    with pytest.raises(ValidationError):
        validate_entry.check_repository_allowlisted("oci:///ocx-contrib/cmake")


def test_check_repository_allowlisted_hostless_netloc_raises() -> None:
    # netloc is non-empty (":8080") but urlsplit resolves no hostname from it.
    with pytest.raises(ValidationError):
        validate_entry.check_repository_allowlisted("oci://:8080/ocx-contrib/cmake")


def test_check_repository_allowlisted_unlisted_host_raises() -> None:
    with pytest.raises(ValidationError, match="G-03"):
        validate_entry.check_repository_allowlisted("oci://evil.example.com/ocx-contrib/cmake")


class _PoisonRegistry:
    """A `RegistryPort` whose every method raises — proves a code path never
    reaches for the network (SSRF ordering, ADR-4 BD-1)."""

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


def test_ssrf_ordering_allowlist_rejection_precedes_every_registry_call() -> None:
    """Simulates the real caller pipeline (`cli/validate.py`): allowlist
    check first, network-touching checks only afterward. A poisoned registry
    that raises on any call proves zero port calls happen once the allowlist
    check has already rejected the repository.
    """
    poison = _PoisonRegistry()
    bad_repository = "oci://evil.example.com/ocx-contrib/cmake"

    with pytest.raises(ValidationError, match="G-03"):
        validate_entry.check_repository_allowlisted(bad_repository)
        # Unreachable below by construction — documents that a correctly
        # ordered pipeline never gets far enough to touch `poison`.
        check_digest_in_scope(bad_repository, "sha256:" + "a" * 64, poison)


# --- check_repository_shape --------------------------------------------------


def test_check_repository_shape_ok() -> None:
    validate_entry.check_repository_shape("oci://ghcr.io/ocx-contrib/cmake")


def test_check_repository_shape_multi_segment_ok() -> None:
    validate_entry.check_repository_shape("oci://ghcr.io/ocx-contrib/nested/cmake")


def test_check_repository_shape_empty_path_raises() -> None:
    with pytest.raises(ValidationError):
        validate_entry.check_repository_shape("oci://ghcr.io")


def test_check_repository_shape_uppercase_raises() -> None:
    with pytest.raises(ValidationError):
        validate_entry.check_repository_shape("oci://ghcr.io/Ocx-Contrib/CMake")


def test_check_repository_shape_empty_segment_raises() -> None:
    with pytest.raises(ValidationError):
        validate_entry.check_repository_shape("oci://ghcr.io/ocx-contrib//cmake")


# --- parse_digest -------------------------------------------------------------


def test_parse_digest_ok() -> None:
    digest = "sha256:" + "a" * 64
    assert validate_entry.parse_digest(digest) == digest


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "sha256:",
        "sha256:" + "a" * 63,  # too short
        "sha256:" + "a" * 65,  # too long
        "sha256:" + "A" * 64,  # uppercase hex not allowed
        "sha256:" + "g" * 64,  # non-hex char
        "md5:" + "a" * 32,  # wrong algorithm
        "sha256:" + "a" * 64 + "/../../etc/passwd",  # path traversal suffix
        "../../../etc/passwd",  # path traversal, no digest shape at all
        "sha256:" + "a" * 64 + "\x00",  # embedded NUL
    ],
)
def test_parse_digest_rejects_malformed_and_malicious_input(raw: str) -> None:
    with pytest.raises(ValidationError):
        validate_entry.parse_digest(raw)


# --- cas_relpath (finding #6: re-add the direct unit test lost in the
# core/catalog_md.py -> validate_entry.py relocation) -----------------------


def test_cas_relpath_builds_the_p_ns_pkg_o_sha256_hex_ext_shape() -> None:
    digest = "sha256:" + "a" * 64
    assert (
        validate_entry.cas_relpath("kitware", "cmake", digest, "json")
        == f"p/kitware/cmake/o/sha256/{'a' * 64}.json"
    )


def test_cas_relpath_varies_extension() -> None:
    digest = "sha256:" + "b" * 64
    assert validate_entry.cas_relpath("kitware", "cmake", digest, "md").endswith(".md")
    assert validate_entry.cas_relpath("kitware", "cmake", digest, "svg").endswith(".svg")


# --- check_content_digest_self_consistent ------------------------------------


def test_check_content_digest_self_consistent_ok() -> None:
    object_bytes = b'{"platforms":[]}'
    digest = f"sha256:{hashlib.sha256(object_bytes).hexdigest()}"
    tag = TagEntry(content=digest, observed="2026-07-17T00:00:00Z")
    validate_entry.check_content_digest_self_consistent(tag, object_bytes)  # no raise


def test_check_content_digest_self_consistent_mismatch_raises_anomaly() -> None:
    object_bytes = b'{"platforms":[]}'
    tag = TagEntry(content="sha256:" + "0" * 64, observed="2026-07-17T00:00:00Z")
    with pytest.raises(AnomalyError):
        validate_entry.check_content_digest_self_consistent(tag, object_bytes)


# --- check_no_dangling_references --------------------------------------------


def test_check_no_dangling_references_clean() -> None:
    digest = "sha256:" + "a" * 64
    root = _minimal_root(tags={"latest": TagEntry(content=digest, observed="2026-07-17T00:00:00Z")})
    validate_entry.check_no_dangling_references(root, frozenset({digest}))  # no raise


def test_check_no_dangling_references_missing_tag_content_raises() -> None:
    digest = "sha256:" + "a" * 64
    root = _minimal_root(tags={"latest": TagEntry(content=digest, observed="2026-07-17T00:00:00Z")})
    with pytest.raises(AnomalyError, match="tags\\[latest\\]"):
        validate_entry.check_no_dangling_references(root, frozenset())


def test_check_no_dangling_references_missing_desc_readme_raises() -> None:
    digest = "sha256:" + "a" * 64
    readme_digest = "sha256:" + "b" * 64
    desc = Desc(digest=digest, title="CMake", description="build tool", readme=readme_digest)
    root = _minimal_root(desc=desc)
    with pytest.raises(AnomalyError, match=r"desc\.readme"):
        validate_entry.check_no_dangling_references(root, frozenset())


def test_check_no_dangling_references_missing_desc_logo_raises() -> None:
    digest = "sha256:" + "a" * 64
    logo_digest = "sha256:" + "c" * 64
    desc = Desc(digest=digest, title="CMake", description="build tool", logo=logo_digest)
    root = _minimal_root(desc=desc)
    with pytest.raises(AnomalyError, match=r"desc\.logo"):
        validate_entry.check_no_dangling_references(root, frozenset())


def test_check_no_dangling_references_desc_none_only_checks_tags() -> None:
    digest = "sha256:" + "a" * 64
    root = _minimal_root(
        desc=None, tags={"latest": TagEntry(content=digest, observed="2026-07-17T00:00:00Z")}
    )
    validate_entry.check_no_dangling_references(root, frozenset({digest}))  # no raise


def test_check_no_dangling_references_reports_every_missing_reference() -> None:
    tag_digest = "sha256:" + "a" * 64
    readme_digest = "sha256:" + "b" * 64
    logo_digest = "sha256:" + "c" * 64
    desc = Desc(
        digest=tag_digest, title="CMake", description="x", readme=readme_digest, logo=logo_digest
    )
    root = _minimal_root(
        desc=desc, tags={"latest": TagEntry(content=tag_digest, observed="2026-07-17T00:00:00Z")}
    )
    with pytest.raises(AnomalyError) as excinfo:
        validate_entry.check_no_dangling_references(root, frozenset())
    message = str(excinfo.value)
    assert "tags[latest]" in message
    assert "desc.readme" in message
    assert "desc.logo" in message


# --- PackageRoot <-> dict codec ----------------------------------------------


def test_serialize_package_root_full_shape_and_key_order() -> None:
    owner = Owner(github="alice", github_id=123456)
    upstream = Upstream(org="Kitware", repository_url="https://github.com/Kitware/CMake")
    desc = Desc(
        digest="sha256:" + "9" * 64,
        title="CMake",
        description="Cross-platform build system generator.",
        keywords=("build", "cmake", "cpp"),
        readme="sha256:" + "1" * 64,
        logo="sha256:" + "3" * 64,
    )
    tag = TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z")
    root = PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(owner,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=desc,
        upstream=upstream,
        superseded_by="kitware/cmake-ng",
        tags={"3.28.1": tag},
    )
    raw = validate_entry.serialize_package_root(root)
    assert raw.endswith(b"\n")
    parsed = json.loads(raw)
    assert list(parsed.keys()) == [
        "name",
        "repository",
        "owners",
        "status",
        "deprecated_message",
        "created",
        "desc",
        "upstream",
        "superseded_by",
        "tags",
    ]
    assert parsed["upstream"] == {
        "org": "Kitware",
        "repository_url": "https://github.com/Kitware/CMake",
        "disclaimer": None,
    }
    assert parsed["superseded_by"] == "kitware/cmake-ng"


def test_serialize_package_root_omits_upstream_when_none() -> None:
    root = _minimal_root(upstream=None)
    parsed = json.loads(validate_entry.serialize_package_root(root))
    assert "upstream" not in parsed
    assert list(parsed.keys()) == [
        "name",
        "repository",
        "owners",
        "status",
        "deprecated_message",
        "created",
        "desc",
        "tags",
    ]


def test_serialize_package_root_omits_superseded_by_when_none() -> None:
    root = _minimal_root(superseded_by=None)
    parsed = json.loads(validate_entry.serialize_package_root(root))
    assert "superseded_by" not in parsed
    assert list(parsed.keys()) == [
        "name",
        "repository",
        "owners",
        "status",
        "deprecated_message",
        "created",
        "desc",
        "tags",
    ]


def test_serialize_package_root_includes_superseded_by_when_set() -> None:
    root = _minimal_root(superseded_by="kitware/cmake-ng")
    parsed = json.loads(validate_entry.serialize_package_root(root))
    assert parsed["superseded_by"] == "kitware/cmake-ng"


def test_serialize_package_root_writes_desc_null_when_none() -> None:
    root = _minimal_root(desc=None)
    parsed = json.loads(validate_entry.serialize_package_root(root))
    assert parsed["desc"] is None


def test_serialize_package_root_desc_omits_absent_readme_and_logo() -> None:
    desc = Desc(digest="sha256:" + "9" * 64, title="CMake", description="x")
    root = _minimal_root(desc=desc)
    parsed = json.loads(validate_entry.serialize_package_root(root))
    assert "readme" not in parsed["desc"]
    assert "logo" not in parsed["desc"]
    assert parsed["desc"]["keywords"] == []


def test_serialize_package_root_tag_omits_yanked_when_absent_and_includes_when_present() -> None:
    unyanked = TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z")
    yank = Yank(reason="cve", at="2026-07-18T00:00:00Z")
    yanked = TagEntry(content="sha256:" + "b" * 64, observed="2026-07-17T00:00:00Z", yanked=yank)
    root = _minimal_root(tags={"3.28.1": unyanked, "3.27.0": yanked})
    parsed = json.loads(validate_entry.serialize_package_root(root))
    assert "yanked" not in parsed["tags"]["3.28.1"]
    assert parsed["tags"]["3.27.0"]["yanked"] == {"reason": "cve", "at": "2026-07-18T00:00:00Z"}


def test_serialize_package_root_upstream_repository_url_omitted_when_none() -> None:
    root = _minimal_root(upstream=Upstream(org="OCX"))
    parsed = json.loads(validate_entry.serialize_package_root(root))
    assert "repository_url" not in parsed["upstream"]
    assert parsed["upstream"]["disclaimer"] is None


def test_parse_package_root_round_trips_serialize_output() -> None:
    owner = Owner(github="alice", github_id=123456)
    upstream = Upstream(
        org="Kitware", repository_url="https://github.com/Kitware/CMake", disclaimer="Unofficial."
    )
    desc = Desc(
        digest="sha256:" + "9" * 64,
        title="CMake",
        description="Cross-platform build system generator.",
        keywords=("build", "cmake"),
        readme="sha256:" + "1" * 64,
        logo="sha256:" + "3" * 64,
    )
    yank = Yank(reason="cve", at="2026-07-18T00:00:00Z")
    tags = {
        "3.28.1": TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z"),
        "3.27.0": TagEntry(
            content="sha256:" + "b" * 64, observed="2026-07-16T00:00:00Z", yanked=yank
        ),
    }
    root = PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(owner,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=desc,
        upstream=upstream,
        tags=tags,
    )
    raw = validate_entry.serialize_package_root(root)
    round_tripped = validate_entry.parse_package_root(raw)
    assert round_tripped == root


def test_parse_package_root_round_trips_minimal_root_no_upstream_null_desc() -> None:
    root = _minimal_root()
    raw = validate_entry.serialize_package_root(root)
    assert validate_entry.parse_package_root(raw) == root


def test_parse_package_root_superseded_by_absent_key_defaults_to_none() -> None:
    # Older wire payload predating this field entirely -- `.get` fallback,
    # not just the round-trip of this module's own serializer output.
    payload = json.dumps(
        {
            "name": "ocx.sh/kitware/cmake",
            "repository": "oci://ghcr.io/ocx-contrib/cmake",
            "owners": [{"github": "alice", "github_id": 1}],
            "status": "active",
            "deprecated_message": None,
            "created": "2026-07-17",
            "desc": None,
            "tags": {},
        }
    ).encode("utf-8")
    assert validate_entry.parse_package_root(payload).superseded_by is None


def test_parse_package_root_malformed_json_raises() -> None:
    with pytest.raises(ValidationError, match="malformed root JSON"):
        validate_entry.parse_package_root(b"{not json")


def test_parse_package_root_non_object_json_raises() -> None:
    with pytest.raises(ValidationError, match="JSON object"):
        validate_entry.parse_package_root(b"[1, 2, 3]")


def test_parse_package_root_missing_required_key_raises() -> None:
    payload = json.dumps({"name": "ocx.sh/kitware/cmake"}).encode("utf-8")
    with pytest.raises(ValidationError, match="malformed root structure"):
        validate_entry.parse_package_root(payload)


def test_parse_package_root_wrong_type_for_tags_raises() -> None:
    payload = json.dumps(
        {
            "name": "ocx.sh/kitware/cmake",
            "repository": "oci://ghcr.io/ocx-contrib/cmake",
            "owners": [{"github": "alice", "github_id": 1}],
            "status": "active",
            "deprecated_message": None,
            "created": "2026-07-17",
            "desc": None,
            "tags": ["not", "a", "mapping"],
        }
    ).encode("utf-8")
    with pytest.raises(ValidationError, match="malformed root structure"):
        validate_entry.parse_package_root(payload)


def test_parse_package_root_wrong_type_for_owners_raises() -> None:
    payload = json.dumps(
        {
            "name": "ocx.sh/kitware/cmake",
            "repository": "oci://ghcr.io/ocx-contrib/cmake",
            "owners": "not-a-list",
            "status": "active",
            "deprecated_message": None,
            "created": "2026-07-17",
            "desc": None,
            "tags": {},
        }
    ).encode("utf-8")
    with pytest.raises(ValidationError, match="malformed root structure"):
        validate_entry.parse_package_root(payload)


# --- ObservationObject <-> dict codec -----------------------------------------


def test_serialize_observation_object_canonical_minified_shape() -> None:
    obj = ObservationObject(
        platforms=(
            PlatformEntry(
                platform=OciPlatform(architecture="amd64", os="linux"), digest="sha256:" + "1" * 64
            ),
        )
    )
    raw = validate_entry.serialize_observation_object(obj)
    assert raw == (
        b'{"platforms":[{"digest":"sha256:'
        + b"1" * 64
        + b'","platform":{"architecture":"amd64","os":"linux"}}]}'
    )


def test_serialize_observation_object_sorts_platforms_deterministically() -> None:
    entry_amd64 = PlatformEntry(
        platform=OciPlatform(architecture="amd64", os="linux"), digest="sha256:" + "1" * 64
    )
    entry_arm64 = PlatformEntry(
        platform=OciPlatform(architecture="arm64", os="linux"), digest="sha256:" + "2" * 64
    )
    forward = ObservationObject(platforms=(entry_amd64, entry_arm64))
    reversed_order = ObservationObject(platforms=(entry_arm64, entry_amd64))
    assert validate_entry.serialize_observation_object(
        forward
    ) == validate_entry.serialize_observation_object(reversed_order)


def test_serialize_observation_object_full_platform_fields() -> None:
    platform = OciPlatform(
        architecture="arm",
        os="linux",
        os_version="1.0",
        os_features=("sse4",),
        variant="v7",
        features=("f1",),
    )
    obj = ObservationObject(
        platforms=(PlatformEntry(platform=platform, digest="sha256:" + "4" * 64),)
    )
    parsed = json.loads(validate_entry.serialize_observation_object(obj))
    platform_dict = parsed["platforms"][0]["platform"]
    assert platform_dict["os.version"] == "1.0"
    assert platform_dict["os.features"] == ["sse4"]
    assert platform_dict["variant"] == "v7"
    assert platform_dict["features"] == ["f1"]


def test_serialize_observation_object_escapes_non_ascii_and_is_digest_stable() -> None:
    # CONTRACTS.md §1's `ensure_ascii=True` is dedup-load-bearing (ADR-1 D4):
    # a non-ASCII field value must serialize to `\uXXXX` escapes, not raw
    # UTF-8 bytes, and produce the same bytes (hence the same digest) on
    # every call — proven here rather than only asserted by shape.
    platform = OciPlatform(architecture="amd64", os="linux", variant="év7")  # "év7"
    obj = ObservationObject(
        platforms=(PlatformEntry(platform=platform, digest="sha256:" + "8" * 64),)
    )
    raw = validate_entry.serialize_observation_object(obj)
    assert b"\\u00e9v7" in raw
    assert "év7".encode() not in raw
    assert validate_entry.serialize_observation_object(obj) == raw  # stable across calls


def test_serialize_observation_object_minimal_platform_omits_optional_keys() -> None:
    obj = ObservationObject(
        platforms=(
            PlatformEntry(
                platform=OciPlatform(architecture="amd64", os="linux"), digest="sha256:" + "5" * 64
            ),
        )
    )
    parsed = json.loads(validate_entry.serialize_observation_object(obj))
    platform_dict = parsed["platforms"][0]["platform"]
    assert set(platform_dict.keys()) == {"architecture", "os"}


def test_parse_observation_object_round_trips_full_fields() -> None:
    platform = OciPlatform(
        architecture="arm",
        os="linux",
        os_version="1.0",
        os_features=("sse4",),
        variant="v7",
        features=("f1",),
    )
    obj = ObservationObject(
        platforms=(PlatformEntry(platform=platform, digest="sha256:" + "6" * 64),)
    )
    raw = validate_entry.serialize_observation_object(obj)
    assert validate_entry.parse_observation_object(raw) == obj


def test_parse_observation_object_round_trips_minimal_fields() -> None:
    obj = ObservationObject(
        platforms=(
            PlatformEntry(
                platform=OciPlatform(architecture="amd64", os="linux"), digest="sha256:" + "7" * 64
            ),
        )
    )
    raw = validate_entry.serialize_observation_object(obj)
    assert validate_entry.parse_observation_object(raw) == obj


def test_parse_observation_object_malformed_json_raises() -> None:
    with pytest.raises(ValidationError, match="malformed observation object JSON"):
        validate_entry.parse_observation_object(b"{not json")


def test_parse_observation_object_non_object_json_raises() -> None:
    with pytest.raises(ValidationError, match="JSON object"):
        validate_entry.parse_observation_object(b'"just a string"')


def test_parse_observation_object_missing_platforms_key_raises() -> None:
    with pytest.raises(ValidationError, match="malformed observation object structure"):
        validate_entry.parse_observation_object(b"{}")
