from __future__ import annotations

from indexbot.core.anomaly import AnomalyFinding, check_tag_mutations
from indexbot.core.observe import Observation
from indexbot.model import Owner, PackageId, PackageRoot, TagEntry

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
    return Observation(
        tag=tag, content_digest=digest, raw=b'{"manifests":[{"digest":"' + digest.encode() + b'"}]}'
    )


_PINNED = "3.28.1_20260216120000"


def test_pinned_tag_digest_mutation_is_flagged() -> None:
    committed = _root({_PINNED: TagEntry(content=_DIGEST_A, observed="T0")})
    findings = check_tag_mutations(_PKG, committed, (_observation(_PINNED, _DIGEST_B),))
    assert findings == (
        AnomalyFinding(
            package_id=_PKG, tag=_PINNED, committed_content=_DIGEST_A, fresh_content=_DIGEST_B
        ),
    )


def test_pinned_tag_unchanged_digest_is_clean() -> None:
    committed = _root({_PINNED: TagEntry(content=_DIGEST_A, observed="T0")})
    findings = check_tag_mutations(_PKG, committed, (_observation(_PINNED, _DIGEST_A),))
    assert findings == ()


def test_variant_prefixed_pinned_tag_mutation_is_flagged() -> None:
    # Live shape in this index (`p/astral-sh/python-build-standalone.json`).
    tag = "slim-3.12.13_20260728"
    committed = _root({tag: TagEntry(content=_DIGEST_A, observed="T0")})
    findings = check_tag_mutations(_PKG, committed, (_observation(tag, _DIGEST_B),))
    assert len(findings) == 1


def test_floating_tags_never_flagged_regardless_of_mutation() -> None:
    # Every one of these is a cascade target a legitimate `3.28.1_<build>`
    # push repoints. Flagging any of them turns a correct publish into a
    # tamper issue.
    for tag in ("latest", "3", "3.28", "3.28.1", "slim-3.28.1", "nightly"):
        committed = _root({tag: TagEntry(content=_DIGEST_A, observed="T0")})
        findings = check_tag_mutations(_PKG, committed, (_observation(tag, _DIGEST_B),))
        assert findings == (), f"{tag} must never be flagged"


def test_cascade_publish_moves_every_rolling_tag_and_is_clean() -> None:
    # One republish of 3.28.1 as a new build: the new build tag is not in the
    # committed root yet, the old one still resolves to its own digest, and
    # every rolling ancestor now points at the new index. Zero findings.
    committed = _root(
        {
            _PINNED: TagEntry(content=_DIGEST_A, observed="T0"),
            "3.28.1": TagEntry(content=_DIGEST_A, observed="T0"),
            "3.28": TagEntry(content=_DIGEST_A, observed="T0"),
            "3": TagEntry(content=_DIGEST_A, observed="T0"),
            "latest": TagEntry(content=_DIGEST_A, observed="T0"),
        }
    )
    fresh = (
        _observation(_PINNED, _DIGEST_A),
        _observation("3.28.1", _DIGEST_B),
        _observation("3.28", _DIGEST_B),
        _observation("3", _DIGEST_B),
        _observation("latest", _DIGEST_B),
    )
    assert check_tag_mutations(_PKG, committed, fresh) == ()


def test_tag_absent_from_fresh_observations_is_not_flagged() -> None:
    committed = _root({_PINNED: TagEntry(content=_DIGEST_A, observed="T0")})
    findings = check_tag_mutations(_PKG, committed, ())
    assert findings == ()


def test_multiple_pinned_mutations_all_reported() -> None:
    committed = _root(
        {
            "3.28.1_20260216120000": TagEntry(content=_DIGEST_A, observed="T0"),
            "3.29.0_20260216120000": TagEntry(content=_DIGEST_A, observed="T0"),
        }
    )
    findings = check_tag_mutations(
        _PKG,
        committed,
        (
            _observation("3.28.1_20260216120000", _DIGEST_B),
            _observation("3.29.0_20260216120000", _DIGEST_B),
        ),
    )
    assert len(findings) == 2
