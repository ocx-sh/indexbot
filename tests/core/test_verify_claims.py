from __future__ import annotations

import hashlib

from indexbot.core.observe import observe_one_tag
from indexbot.core.validate_entry import serialize_observation_object
from indexbot.core.verify_claims import ClaimFinding, verify_claims
from indexbot.model import Desc, Owner, PackageId, PackageRoot, TagEntry
from tests.fakes import FakeRegistry

_PACKAGE_ID = PackageId(namespace="kitware", package="cmake")
_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_OWNER = Owner(github="alice", github_id=1)


def _root(tags: dict[str, TagEntry] | None = None, *, desc: Desc | None = None) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository=_REPO,
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=desc,
        tags=dict(tags or {}),
    )


def _bare_manifest(architecture: str = "amd64") -> dict[str, object]:
    return {"platform": {"architecture": architecture, "os": "linux"}}


def _observed_claim(
    tag: str, *, architecture: str = "amd64"
) -> tuple[TagEntry, bytes, FakeRegistry]:
    """A `TagEntry` + its CAS object bytes + a `FakeRegistry` that
    independently re-derives to the exact same content digest — the clean
    baseline every test starts from and mutates one field of."""
    registry = FakeRegistry(
        tags={_REPO: [tag]}, manifests={(_REPO, tag): _bare_manifest(architecture)}
    )
    observation = observe_one_tag(_REPO, tag, registry)
    assert observation is not None
    object_bytes = serialize_observation_object(observation.object)
    entry = TagEntry(content=observation.content_digest, observed="2026-07-17T00:00:00Z")
    return entry, object_bytes, registry


def test_verify_claims_clean_tag_is_empty() -> None:
    entry, object_bytes, registry = _observed_claim("3.28.1")
    root = _root({"3.28.1": entry})
    findings = verify_claims(_PACKAGE_ID, root, {entry.content: object_bytes}, registry)
    assert findings == ()


def test_verify_claims_no_tags_no_desc_is_empty() -> None:
    root = _root({})
    findings = verify_claims(_PACKAGE_ID, root, {}, FakeRegistry())
    assert findings == ()


def test_verify_claims_tag_missing_upstream() -> None:
    entry, object_bytes, _registry = _observed_claim("3.28.1")
    root = _root({"3.28.1": entry})
    # A registry with no manifest at all for this tag -> observe_one_tag
    # returns None -> the tag no longer resolves upstream.
    findings = verify_claims(_PACKAGE_ID, root, {entry.content: object_bytes}, FakeRegistry())
    assert findings == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="tag-missing-upstream", detail="3.28.1"),
    )


def test_verify_claims_digest_mismatch() -> None:
    entry, object_bytes, registry = _observed_claim("3.28.1")
    stale_entry = TagEntry(content="sha256:" + "0" * 64, observed="2026-07-17T00:00:00Z")
    root = _root({"3.28.1": stale_entry})
    findings = verify_claims(
        _PACKAGE_ID,
        root,
        {stale_entry.content: object_bytes, entry.content: object_bytes},
        registry,
    )
    assert findings == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="digest-mismatch", detail="3.28.1"),
    )


def test_verify_claims_cas_object_missing() -> None:
    entry, _object_bytes, registry = _observed_claim("3.28.1")
    root = _root({"3.28.1": entry})
    findings = verify_claims(_PACKAGE_ID, root, {}, registry)
    assert findings == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="cas-object-missing", detail="3.28.1"),
    )


def test_verify_claims_cas_object_hash_mismatch() -> None:
    entry, _object_bytes, registry = _observed_claim("3.28.1")
    root = _root({"3.28.1": entry})
    findings = verify_claims(_PACKAGE_ID, root, {entry.content: b"tampered bytes"}, registry)
    assert findings == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="cas-object-hash-mismatch", detail="3.28.1"),
    )


def test_verify_claims_multiple_tags_sorted_by_name() -> None:
    entry_a, bytes_a, registry = _observed_claim("1.0.0", architecture="amd64")
    entry_b, bytes_b, registry_b = _observed_claim("2.0.0", architecture="arm64")
    registry.tags[_REPO] = ["1.0.0", "2.0.0"]
    registry.manifests.update(registry_b.manifests)
    root = _root({"2.0.0": entry_b, "1.0.0": entry_a})
    findings = verify_claims(
        _PACKAGE_ID, root, {entry_a.content: bytes_a, entry_b.content: bytes_b}, registry
    )
    assert findings == ()


def test_verify_claims_desc_readme_missing() -> None:
    desc = Desc(
        digest="sha256:" + "d" * 64, title="CMake", description="x", readme="sha256:" + "e" * 64
    )
    root = _root({}, desc=desc)
    findings = verify_claims(_PACKAGE_ID, root, {}, FakeRegistry())
    assert findings == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="desc-blob-missing", detail="desc.readme"),
    )


def test_verify_claims_desc_readme_hash_mismatch() -> None:
    readme_digest = "sha256:" + "e" * 64
    desc = Desc(digest="sha256:" + "d" * 64, title="CMake", description="x", readme=readme_digest)
    root = _root({}, desc=desc)
    findings = verify_claims(_PACKAGE_ID, root, {readme_digest: b"not the readme"}, FakeRegistry())
    assert findings == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="desc-blob-hash-mismatch", detail="desc.readme"),
    )


def test_verify_claims_desc_readme_clean() -> None:
    readme_bytes = b"# CMake\n"
    readme_digest = f"sha256:{hashlib.sha256(readme_bytes).hexdigest()}"
    desc = Desc(digest="sha256:" + "d" * 64, title="CMake", description="x", readme=readme_digest)
    root = _root({}, desc=desc)
    findings = verify_claims(_PACKAGE_ID, root, {readme_digest: readme_bytes}, FakeRegistry())
    assert findings == ()


def test_verify_claims_desc_logo_missing() -> None:
    logo_digest = "sha256:" + "f" * 64
    desc = Desc(
        digest="sha256:" + "d" * 64,
        title="CMake",
        description="x",
        readme=None,
        logo=logo_digest,
    )
    root = _root({}, desc=desc)
    findings = verify_claims(_PACKAGE_ID, root, {}, FakeRegistry())
    assert findings == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="desc-blob-missing", detail="desc.logo"),
    )


def test_verify_claims_desc_logo_hash_mismatch() -> None:
    logo_digest = "sha256:" + "f" * 64
    desc = Desc(digest="sha256:" + "d" * 64, title="CMake", description="x", logo=logo_digest)
    root = _root({}, desc=desc)
    findings = verify_claims(_PACKAGE_ID, root, {logo_digest: b"not the logo"}, FakeRegistry())
    assert findings == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="desc-blob-hash-mismatch", detail="desc.logo"),
    )


def test_verify_claims_desc_readme_and_logo_both_clean() -> None:
    readme_bytes = b"# CMake\n"
    logo_bytes = b"<svg></svg>"
    readme_digest = f"sha256:{hashlib.sha256(readme_bytes).hexdigest()}"
    logo_digest = f"sha256:{hashlib.sha256(logo_bytes).hexdigest()}"
    desc = Desc(
        digest="sha256:" + "d" * 64,
        title="CMake",
        description="x",
        readme=readme_digest,
        logo=logo_digest,
    )
    root = _root({}, desc=desc)
    findings = verify_claims(
        _PACKAGE_ID, root, {readme_digest: readme_bytes, logo_digest: logo_bytes}, FakeRegistry()
    )
    assert findings == ()


def test_verify_claims_desc_none_is_a_noop() -> None:
    root = _root({}, desc=None)
    findings = verify_claims(_PACKAGE_ID, root, {}, FakeRegistry())
    assert findings == ()
