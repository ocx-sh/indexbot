from __future__ import annotations

from indexbot.core.anomaly import AnomalyFinding, check_tag_mutations
from indexbot.core.observe import Observation
from indexbot.model import (
    ObservationObject,
    OciPlatform,
    Owner,
    PackageId,
    PackageRoot,
    PlatformEntry,
    TagEntry,
)

_OWNER = Owner(github="alice", github_id=1)
_PKG = PackageId(namespace="kitware", package="cmake")
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _root(tags: dict[str, TagEntry]) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags),
    )


def _observation(tag: str, digest: str) -> Observation:
    platform = OciPlatform(architecture="amd64", os="linux")
    entry = PlatformEntry(platform=platform, digest="sha256:" + "1" * 64)
    return Observation(tag=tag, content_digest=digest, object=ObservationObject(platforms=(entry,)))


def test_pinned_tag_digest_mutation_is_flagged() -> None:
    committed = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    findings = check_tag_mutations(_PKG, committed, (_observation("3.28.1", _DIGEST_B),))
    assert findings == (
        AnomalyFinding(
            package_id=_PKG, tag="3.28.1", committed_content=_DIGEST_A, fresh_content=_DIGEST_B
        ),
    )


def test_pinned_tag_unchanged_digest_is_clean() -> None:
    committed = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    findings = check_tag_mutations(_PKG, committed, (_observation("3.28.1", _DIGEST_A),))
    assert findings == ()


def test_floating_tags_never_flagged_regardless_of_mutation() -> None:
    for tag in ("latest", "3", "3.28", "nightly-1.2.3"):
        committed = _root({tag: TagEntry(content=_DIGEST_A, observed="T0")})
        findings = check_tag_mutations(_PKG, committed, (_observation(tag, _DIGEST_B),))
        assert findings == (), f"{tag} must never be flagged"


def test_tag_absent_from_fresh_observations_is_not_flagged() -> None:
    committed = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    findings = check_tag_mutations(_PKG, committed, ())
    assert findings == ()


def test_multiple_pinned_mutations_all_reported() -> None:
    committed = _root(
        {
            "3.28.1": TagEntry(content=_DIGEST_A, observed="T0"),
            "3.29.0": TagEntry(content=_DIGEST_A, observed="T0"),
        }
    )
    findings = check_tag_mutations(
        _PKG,
        committed,
        (_observation("3.28.1", _DIGEST_B), _observation("3.29.0", _DIGEST_B)),
    )
    assert len(findings) == 2
