from __future__ import annotations

from indexbot.core.diff import classify_change, diff
from indexbot.core.observe import Observation
from indexbot.model import (
    ObservationObject,
    OciPlatform,
    Owner,
    PackageId,
    PackageRoot,
    PlatformEntry,
    Status,
    TagEntry,
    Yank,
)

_OWNER = Owner(github="alice", github_id=1)
_PKG = PackageId(namespace="kitware", package="cmake")
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _root(
    tags: dict[str, TagEntry],
    *,
    status: Status = "active",
    repository: str = "oci://ghcr.io/ocx-contrib/cmake",
) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository=repository,
        owners=(_OWNER,),
        status=status,
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags),
    )


def _observation(tag: str, digest: str) -> Observation:
    platform = OciPlatform(architecture="amd64", os="linux")
    entry = PlatformEntry(platform=platform, digest="sha256:" + "1" * 64)
    return Observation(tag=tag, content_digest=digest, object=ObservationObject(platforms=(entry,)))


def test_diff_returns_none_for_identical_roots() -> None:
    root = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    assert diff(_PKG, root, root, (_observation("3.28.1", _DIGEST_A),)) is None


def test_diff_new_objects_excludes_already_reachable_digests() -> None:
    current = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    target = _root(
        {
            "3.28.1": TagEntry(content=_DIGEST_A, observed="T0"),
            "3.29.0": TagEntry(content=_DIGEST_B, observed="T1"),
        }
    )
    observations = (_observation("3.28.1", _DIGEST_A), _observation("3.29.0", _DIGEST_B))
    patch = diff(_PKG, current, target, observations)
    assert patch is not None
    assert [digest for digest, _ in patch.new_objects] == [_DIGEST_B]


def test_diff_shared_digest_cascade_produces_no_new_objects() -> None:
    current = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    target = _root(
        {
            "3.28.1": TagEntry(content=_DIGEST_A, observed="T0"),
            "latest": TagEntry(content=_DIGEST_A, observed="T1"),
        }
    )
    observations = (_observation("3.28.1", _DIGEST_A), _observation("latest", _DIGEST_A))
    patch = diff(_PKG, current, target, observations)
    assert patch is not None
    assert patch.new_objects == ()


def test_diff_summary_reports_additions_changes_removals() -> None:
    current = _root(
        {
            "3.28.0": TagEntry(content=_DIGEST_A, observed="T0"),
            "latest": TagEntry(content=_DIGEST_A, observed="T0"),
        }
    )
    target = _root(
        {
            "latest": TagEntry(content=_DIGEST_B, observed="T1"),
            "3.29.0": TagEntry(content=_DIGEST_B, observed="T1"),
        }
    )
    observations = (_observation("latest", _DIGEST_B), _observation("3.29.0", _DIGEST_B))
    patch = diff(_PKG, current, target, observations)
    assert patch is not None
    assert "+3.29.0" in patch.summary
    assert "-3.28.0" in patch.summary
    assert "~latest" in patch.summary


def test_diff_metadata_only_change_has_fallback_summary() -> None:
    current = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")}, status="active")
    target = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")}, status="deprecated")
    patch = diff(_PKG, current, target, ())
    assert patch is not None
    assert patch.summary == "metadata updated"
    assert patch.new_objects == ()


def test_diff_patch_carries_package_id_and_target_root() -> None:
    current = _root({})
    target = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    patch = diff(_PKG, current, target, (_observation("3.28.1", _DIGEST_A),))
    assert patch is not None
    assert patch.package_id == _PKG
    assert patch.root is target


def test_classify_change_before_none_is_new_package() -> None:
    after = _root({})
    assert classify_change(None, after) == "new-package"


def test_classify_change_repository_diff_is_human_review() -> None:
    before = _root({}, repository="oci://ghcr.io/ocx-contrib/cmake")
    after = _root({}, repository="oci://ghcr.io/ocx-contrib/cmake2")
    assert classify_change(before, after) == "human-review-required"


def test_classify_change_yanked_diff_is_human_review() -> None:
    before = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    after = _root(
        {"3.28.1": TagEntry(content=_DIGEST_A, observed="T0", yanked=Yank(reason="cve", at="T1"))}
    )
    assert classify_change(before, after) == "human-review-required"


def test_classify_change_content_only_diff_is_refresh() -> None:
    before = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    after = _root({"3.28.1": TagEntry(content=_DIGEST_B, observed="T1")})
    assert classify_change(before, after) == "refresh"


def test_classify_change_new_tag_only_is_refresh() -> None:
    before = _root({})
    after = _root({"3.28.1": TagEntry(content=_DIGEST_A, observed="T0")})
    assert classify_change(before, after) == "refresh"
