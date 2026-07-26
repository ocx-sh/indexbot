from __future__ import annotations

import hashlib

from indexbot.core.observe import observe_one_tag
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


def _index(architecture: str = "amd64") -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "manifests": [
            {
                "platform": {"architecture": architecture, "os": "linux"},
                "digest": "sha256:" + "1" * 64,
            }
        ],
    }


def _observed_claim(
    tag: str, *, architecture: str = "amd64"
) -> tuple[TagEntry, bytes, FakeRegistry]:
    """A `TagEntry` + the registry's own index bytes + a `FakeRegistry` still
    serving them — the clean baseline every test starts from and mutates one
    field of."""
    registry = FakeRegistry(tags={_REPO: [tag]}, manifests={(_REPO, tag): _index(architecture)})
    observation = observe_one_tag(_REPO, tag, registry)
    assert observation is not None
    entry = TagEntry(content=observation.content_digest, observed="2026-07-17T00:00:00Z")
    return entry, observation.raw, registry


def test_verify_claims_clean_tag_is_empty() -> None:
    entry, object_bytes, registry = _observed_claim("3.28.1")
    root = _root({"3.28.1": entry})
    findings = verify_claims(_PACKAGE_ID, root, {entry.content: object_bytes}, registry)
    assert findings == ()


def test_verify_tag_claim_compares_against_the_registry_computed_digest() -> None:
    """The claimed `content` is compared to `ManifestFetch.digest` — the
    registry's own digest over the bytes it served — not to anything this bot
    re-derived. Equal digest, equal bytes, no finding; and the committed CAS
    bytes are literally the ones the registry returned."""
    entry, object_bytes, registry = _observed_claim("3.28.1")
    assert entry.content == registry.get_manifest(_REPO, "3.28.1").digest
    assert object_bytes == registry.get_manifest(_REPO, "3.28.1").raw
    assert (
        verify_claims(
            _PACKAGE_ID, _root({"3.28.1": entry}), {entry.content: object_bytes}, registry
        )
        == ()
    )


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


def test_verify_claims_retagged_to_a_bare_manifest_is_a_digest_mismatch() -> None:
    """The publisher re-tagged `3.28.1` onto a single image manifest.
    `observe_one_tag` raises `ValidationError` for that, but this module
    returns findings and never raises: the claim "this tag resolves to the
    committed index" stopped being true, which is `"digest-mismatch"`.

    Letting the raise through would drop the exact drift the nightly sweep
    exists to detect, exit on the `ValidationError` arm instead of the
    anomaly arm, and skip every package queued behind this one."""
    entry, object_bytes, _registry = _observed_claim("3.28.1")
    retagged = FakeRegistry(
        tags={_REPO: ["3.28.1"]},
        manifests={(_REPO, "3.28.1"): {"config": {"digest": "sha256:" + "9" * 64}}},
    )
    findings = verify_claims(
        _PACKAGE_ID, _root({"3.28.1": entry}), {entry.content: object_bytes}, retagged
    )
    assert findings == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="digest-mismatch", detail="3.28.1"),
    )


def test_verify_claims_keeps_verifying_after_a_rejected_tag() -> None:
    """One bad tag must not abandon the packages/tags behind it — the
    concrete cost of an escaping exception."""
    good_entry, good_bytes, _r = _observed_claim("3.27.0")
    bad_entry, bad_bytes, _r2 = _observed_claim("3.28.1")
    registry = FakeRegistry(
        tags={_REPO: ["3.27.0", "3.28.1"]},
        manifests={
            (_REPO, "3.27.0"): _index(),
            (_REPO, "3.28.1"): {"config": {"digest": "sha256:" + "9" * 64}},
        },
    )
    findings = verify_claims(
        _PACKAGE_ID,
        _root({"3.27.0": good_entry, "3.28.1": bad_entry}),
        {good_entry.content: good_bytes, bad_entry.content: bad_bytes},
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
