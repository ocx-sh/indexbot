from __future__ import annotations

from indexbot.core.observe import Observation
from indexbot.core.regenerate import regenerate
from indexbot.model import (
    Desc,
    ObservationObject,
    OciPlatform,
    Owner,
    PackageRoot,
    PlatformEntry,
    TagEntry,
    Upstream,
    Yank,
)
from tests.fakes import FixedClock

_OWNER = Owner(github="alice", github_id=1)
_UPSTREAM = Upstream(org="Kitware")
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
        upstream=_UPSTREAM,
        desc=None,
        tags=dict(tags),
    )


def _observation(tag: str, digest: str) -> Observation:
    platform = OciPlatform(architecture="amd64", os="linux")
    entry = PlatformEntry(platform=platform, digest="sha256:" + "1" * 64)
    return Observation(tag=tag, content_digest=digest, object=ObservationObject(platforms=(entry,)))


def test_regenerate_new_tag_gets_fresh_timestamp() -> None:
    current = _root({})
    result = regenerate(current, (_observation("3.28.1", _DIGEST_A),), None, FixedClock(fixed="T1"))
    assert result.tags["3.28.1"] == TagEntry(content=_DIGEST_A, observed="T1")


def test_regenerate_unchanged_tag_keeps_observed_timestamp() -> None:
    existing = TagEntry(content=_DIGEST_A, observed="T0")
    current = _root({"3.28.1": existing})
    result = regenerate(current, (_observation("3.28.1", _DIGEST_A),), None, FixedClock(fixed="T1"))
    assert result.tags["3.28.1"] is existing


def test_regenerate_changed_content_gets_fresh_timestamp() -> None:
    existing = TagEntry(content=_DIGEST_A, observed="T0")
    current = _root({"3.28.1": existing})
    result = regenerate(current, (_observation("3.28.1", _DIGEST_B),), None, FixedClock(fixed="T1"))
    assert result.tags["3.28.1"] == TagEntry(content=_DIGEST_B, observed="T1")


def test_regenerate_preserves_yanked_marker_across_content_change() -> None:
    yank = Yank(reason="cve", at="T0")
    existing = TagEntry(content=_DIGEST_A, observed="T0", yanked=yank)
    current = _root({"3.28.1": existing})
    result = regenerate(current, (_observation("3.28.1", _DIGEST_B),), None, FixedClock(fixed="T1"))
    assert result.tags["3.28.1"].yanked is yank


def test_regenerate_drops_tag_absent_from_observations() -> None:
    existing = TagEntry(content=_DIGEST_A, observed="T0")
    current = _root({"3.28.1": existing})
    result = regenerate(current, (), None, FixedClock(fixed="T1"))
    assert result.tags == {}


def test_regenerate_carries_over_governance_fields_verbatim() -> None:
    current = _root({})
    result = regenerate(current, (), None, FixedClock(fixed="T1"))
    assert result.name == current.name
    assert result.repository == current.repository
    assert result.owners == current.owners
    assert result.status == current.status
    assert result.deprecated_message == current.deprecated_message
    assert result.created == current.created
    assert result.upstream == current.upstream


def test_regenerate_uses_caller_supplied_desc() -> None:
    current = _root({})
    new_desc = Desc(digest="sha256:" + "c" * 64, title="CMake", description="Build tool")
    result = regenerate(current, (), new_desc, FixedClock(fixed="T1"))
    assert result.desc is new_desc


def test_regenerate_idempotent_run_twice_no_timestamp_churn() -> None:
    current = _root({})
    observations = (_observation("3.28.1", _DIGEST_A),)
    first = regenerate(current, observations, None, FixedClock(fixed="T1"))
    second = regenerate(first, observations, None, FixedClock(fixed="T2"))
    assert second.tags["3.28.1"].observed == "T1"
    assert second == first
