from __future__ import annotations

from indexbot.core.version_order import variant_names


def test_no_variant_prefixed_tag_yields_no_variants() -> None:
    assert variant_names(["latest", "3", "3.28", "3.28.1"]) == ()


def test_a_variant_prefixed_tag_yields_its_prefix() -> None:
    assert variant_names(["3.28.1", "slim-3.28.1"]) == ("slim",)


def test_variants_are_sorted_and_deduplicated() -> None:
    tags = ["musl-3", "slim-3.28", "musl-3.28.1", "slim-3", "3.28.1"]
    assert variant_names(tags) == ("musl", "slim")


def test_latest_is_reserved_and_never_a_variant() -> None:
    # `latest-3.28.1` parses shape-wise, but `latest` is reserved: the Rust
    # `Version::parse` returns `None` for it outright, so the tag is not a
    # version at all and contributes no variant.
    assert variant_names(["latest-3.28.1"]) == ()


def test_a_bare_variant_rolling_tag_alone_is_not_a_variant() -> None:
    # `slim` on its own is not a version, so the derivation cannot see it.
    # It becomes legible only alongside a versioned `slim-*` tag, and even
    # then this function reads the versioned tag, never the bare one.
    assert variant_names(["slim", "latest"]) == ()
    assert variant_names(["slim", "slim-3.28.1"]) == ("slim",)


def test_prerelease_and_build_suffixes_still_carry_their_variant() -> None:
    # The regression `_VERSION_RE` would cause if it were reused here: it has
    # no prerelease/build groups, so both of these would fail to parse and
    # `slim` would silently vanish from the recorded variant set.
    assert variant_names(["slim-3.28.1-rc1"]) == ("slim",)
    assert variant_names(["slim-3.28.1_20260101"]) == ("slim",)
    assert variant_names(["slim-3.28.1+20260101"]) == ("slim",)


def test_a_dotted_variant_name_is_one_variant() -> None:
    assert variant_names(["pgo.lto-3.28.1"]) == ("pgo.lto",)


def test_non_version_tags_contribute_nothing() -> None:
    assert variant_names(["__ocx.desc", "v3.28.1", "nightly", "SLIM-3.28.1"]) == ()


def test_a_leading_zero_component_is_not_a_version() -> None:
    # `01` is rejected by the version grammar, so the whole tag is not a
    # version and its prefix is not a variant.
    assert variant_names(["slim-01.2.3"]) == ()
