from __future__ import annotations

import hashlib

from ocx_indexbot.core.observe import observe_one_tag
from ocx_indexbot.core.verify_claims import ClaimFinding, verify_claims
from ocx_indexbot.model import Desc, Owner, PackageId, PackageRoot, TagEntry
from tests.fakes import FakeRegistry

_PACKAGE_ID = PackageId(segments=("kitware", "cmake"))
_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_OWNER = Owner(login="alice", id=1)


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


# --- carried-over claims (the `base` narrowing) -----------------------------


def _drifted(tag: str) -> tuple[TagEntry, bytes, FakeRegistry]:
    """A committed claim whose tag has since MOVED upstream: the returned
    entry/bytes are what the index committed, the registry now serves a
    different image index under the same tag.

    The real shape of the twelve `ocx-sh/index` packages whose `latest` and
    partial-version tags drifted between announce and the next unrelated
    pull request.
    """
    entry, object_bytes, registry = _observed_claim(tag)
    registry.manifests[(_REPO, tag)] = _index("arm64")
    assert registry.get_manifest(_REPO, tag).digest != entry.content
    return entry, object_bytes, registry


def test_a_drifted_tag_the_pr_did_not_touch_is_not_this_prs_problem() -> None:
    entry, object_bytes, registry = _drifted("latest")
    root = _root({"latest": entry})
    assert (
        verify_claims(_PACKAGE_ID, root, {entry.content: object_bytes}, registry, base=root) == ()
    )


def test_the_same_drift_is_still_reported_with_no_base_ref_copy() -> None:
    # The `base=None` half of the pair above: a gate with nothing to compare
    # against verifies everything, so the narrowing can only ever be reached
    # deliberately.
    entry, object_bytes, registry = _drifted("latest")
    root = _root({"latest": entry})
    assert verify_claims(_PACKAGE_ID, root, {entry.content: object_bytes}, registry) == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="digest-mismatch", detail="latest"),
    )


def test_a_tag_the_pr_repoints_is_verified_even_though_the_base_carries_it() -> None:
    # The attack the narrowing must not open: same tag name, different digest.
    # Only a byte-identical pair is carried over.
    entry, object_bytes, registry = _drifted("latest")
    base = _root({"latest": TagEntry(content="sha256:" + "9" * 64, observed=entry.observed)})
    assert verify_claims(
        _PACKAGE_ID, _root({"latest": entry}), {entry.content: object_bytes}, registry, base=base
    ) == (ClaimFinding(package_id=_PACKAGE_ID, kind="digest-mismatch", detail="latest"),)


def test_a_tag_the_pr_adds_is_verified_in_full() -> None:
    entry, object_bytes, registry = _drifted("latest")
    assert verify_claims(
        _PACKAGE_ID,
        _root({"latest": entry}),
        {entry.content: object_bytes},
        registry,
        base=_root({}),
    ) == (ClaimFinding(package_id=_PACKAGE_ID, kind="digest-mismatch", detail="latest"),)


def test_a_carried_over_claim_still_has_its_cas_bytes_checked() -> None:
    # The half that stays unconditional: skipping the REGISTRY re-derivation
    # must not let a pull request delete a committed CAS object while keeping
    # the root's claim to it.
    entry, _, registry = _drifted("latest")
    root = _root({"latest": entry})
    assert verify_claims(_PACKAGE_ID, root, {}, registry, base=root) == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="cas-object-missing", detail="latest"),
    )


def test_a_carried_over_claim_still_has_its_cas_bytes_hash_checked() -> None:
    entry, _, registry = _drifted("latest")
    root = _root({"latest": entry})
    assert verify_claims(_PACKAGE_ID, root, {entry.content: b"tampered"}, registry, base=root) == (
        ClaimFinding(package_id=_PACKAGE_ID, kind="cas-object-hash-mismatch", detail="latest"),
    )


def test_repointing_the_repository_discards_every_carried_over_claim() -> None:
    # A root whose `repository` moved resolves every tag against a different
    # physical registry, so no claim under it was ever verified — carrying
    # them over would trust digests observed somewhere else entirely.
    entry, object_bytes, registry = _drifted("latest")
    base = _root({"latest": entry})
    moved = PackageRoot(
        name=base.name,
        repository="oci://ghcr.io/ocx-contrib/cmake-fork",
        owners=base.owners,
        status=base.status,
        deprecated_message=None,
        created=base.created,
        desc=None,
        tags=dict(base.tags),
    )
    assert verify_claims(
        _PACKAGE_ID, moved, {entry.content: object_bytes}, registry, base=base
    ) == (ClaimFinding(package_id=_PACKAGE_ID, kind="tag-missing-upstream", detail="latest"),)
