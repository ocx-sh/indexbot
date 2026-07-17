from __future__ import annotations

from indexbot.core.version_order import find_latest_version, is_full_release_version
from indexbot.model import TagEntry, Yank


def _tag(content: str, *, yanked: Yank | None = None) -> TagEntry:
    return TagEntry(content=content, observed="2026-07-17T00:00:00Z", yanked=yanked)


def test_is_full_release_version_true_for_exact_x_y_z() -> None:
    assert is_full_release_version("3.28.1") is True


def test_is_full_release_version_false_for_latest() -> None:
    assert is_full_release_version("latest") is False


def test_is_full_release_version_false_for_bare_major() -> None:
    assert is_full_release_version("3") is False


def test_is_full_release_version_false_for_major_minor() -> None:
    assert is_full_release_version("3.28") is False


def test_is_full_release_version_false_for_variant_prefixed() -> None:
    assert is_full_release_version("musl-3.28.1") is False


def test_is_full_release_version_false_for_non_matching_shape() -> None:
    assert is_full_release_version("v3.28.1") is False


def test_is_full_release_version_false_for_trailing_newline() -> None:
    # `$` matches immediately before a trailing "\n" under `.match()`, so
    # this must use `.fullmatch()` — a tag literally named "3.28.1\n" is not
    # the same string as "3.28.1" and must not be classified as pinned.
    assert is_full_release_version("3.28.1\n") is False


def test_find_latest_version_picks_highest_semver() -> None:
    tags = {
        "3.28.1": _tag("sha256:aaaa"),
        "3.29.0": _tag("sha256:bbbb"),
        "3.9.0": _tag("sha256:cccc"),
    }
    assert find_latest_version(tags) == "3.29.0"


def test_find_latest_version_skips_latest_tag() -> None:
    tags = {"latest": _tag("sha256:aaaa"), "1.0.0": _tag("sha256:bbbb")}
    assert find_latest_version(tags) == "1.0.0"


def test_find_latest_version_skips_variant_prefixed_tags() -> None:
    tags = {"1.0.0": _tag("sha256:aaaa"), "musl-9.9.9": _tag("sha256:bbbb")}
    assert find_latest_version(tags) == "1.0.0"


def test_find_latest_version_skips_yanked_tags() -> None:
    tags = {
        "1.0.0": _tag("sha256:aaaa"),
        "2.0.0": _tag("sha256:bbbb", yanked=Yank(reason="cve", at="2026-07-17T00:00:00Z")),
    }
    assert find_latest_version(tags) == "1.0.0"


def test_find_latest_version_skips_non_version_tags() -> None:
    tags = {"nightly": _tag("sha256:aaaa"), "1.0.0": _tag("sha256:bbbb")}
    assert find_latest_version(tags) == "1.0.0"


def test_find_latest_version_partial_versions_compare_by_present_components() -> None:
    # "3.28" -> (3, 28); "3.9.1" -> (3, 9, 1). Tuple comparison: (3, 28) > (3, 9, 1).
    tags = {"3.28": _tag("sha256:aaaa"), "3.9.1": _tag("sha256:bbbb")}
    assert find_latest_version(tags) == "3.28"


def test_find_latest_version_returns_none_when_no_eligible_tag() -> None:
    tags = {"latest": _tag("sha256:aaaa"), "nightly": _tag("sha256:bbbb")}
    assert find_latest_version(tags) is None


def test_find_latest_version_returns_none_for_empty_tags() -> None:
    assert find_latest_version({}) is None


def test_find_latest_version_ignores_trailing_newline_tag_name() -> None:
    # Same `.fullmatch()` discipline as `is_full_release_version` above — a
    # "1.0.0\n" tag name must not be treated as a clean "1.0.0" match.
    tags = {"1.0.0\n": _tag("sha256:aaaa")}
    assert find_latest_version(tags) is None
