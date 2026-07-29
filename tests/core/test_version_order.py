from __future__ import annotations

from indexbot.core.version_order import find_latest_version, is_build_pinned_version
from indexbot.model import TagEntry, Yank


def _tag(content: str, *, yanked: Yank | None = None) -> TagEntry:
    return TagEntry(content=content, observed="2026-07-17T00:00:00Z", yanked=yanked)


def test_is_build_pinned_version_true_for_build_tagged_release() -> None:
    # The shape `ocx package push` writes once and never repoints — the only
    # tag in the whole cascade that is supposed to hold still.
    assert is_build_pinned_version("3.28.1_20260216120000") is True


def test_is_build_pinned_version_true_for_variant_prefixed_build_tag() -> None:
    # `slim-3.12.13_20260728` is live in this index. The cascade preserves the
    # variant prefix, so this leaf is pinned exactly like an unprefixed one.
    assert is_build_pinned_version("slim-3.12.13_20260728") is True


def test_is_build_pinned_version_true_for_prerelease_with_build() -> None:
    # `cascade.rs` gives a prerelease-with-build exactly one cascade level,
    # to its build-less parent `1.0.0-rc1` — so the child is the pinned one.
    assert is_build_pinned_version("1.0.0-rc1_20260728") is True


def test_is_build_pinned_version_true_for_plus_separator() -> None:
    # `Version::parse` accepts `+` and normalizes it to `_`. No OCI tag can
    # carry a `+`, so this can only arrive via a hand-authored root — still a
    # build fragment, still pinned.
    assert is_build_pinned_version("3.28.1+20260216120000") is True


def test_is_build_pinned_version_false_for_rolling_patch() -> None:
    # The regression this whole predicate was inverted on: `3.28.1` is the
    # rolling target a `3.28.1_<build>` push repoints. Flagging it would file
    # a tamper issue against a correct publish.
    assert is_build_pinned_version("3.28.1") is False


def test_is_build_pinned_version_false_for_rolling_ancestors_and_latest() -> None:
    for tag in ("latest", "3", "3.28", "slim", "slim-3.12.13", "1.0.0-rc1"):
        assert is_build_pinned_version(tag) is False, f"{tag} is a cascade target"


def test_is_build_pinned_version_false_for_non_version_shape() -> None:
    # Not a version at all, so it carries no immutability promise even though
    # it contains an underscore — `nightly_20260728` is a publisher's own tag.
    assert is_build_pinned_version("v3.28.1_20260728") is False
    assert is_build_pinned_version("nightly_20260728") is False


def test_is_build_pinned_version_false_for_latest_prefix() -> None:
    # `Version::parse` refuses `latest` as a variant prefix, so this is an
    # opaque tag, not a pinned version. Same rule `variant_names` applies.
    assert is_build_pinned_version("latest-3.28.1_20260728") is False


def test_is_build_pinned_version_false_for_trailing_newline() -> None:
    # `$` matches immediately before a trailing "\n" under `.match()`, so
    # this must use `.fullmatch()` — a tag literally named "3.28.1_1\n" is not
    # the same string as "3.28.1_1" and must not be classified as pinned.
    assert is_build_pinned_version("3.28.1_20260216120000\n") is False


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
    # Same `.fullmatch()` discipline as `is_build_pinned_version` above — a
    # "1.0.0\n" tag name must not be treated as a clean "1.0.0" match.
    tags = {"1.0.0\n": _tag("sha256:aaaa")}
    assert find_latest_version(tags) is None
