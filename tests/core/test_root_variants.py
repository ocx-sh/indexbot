"""`root.variants` end to end: derived by `core/regenerate.py`, spelled by
`core/validate_entry.py`'s codec, and never a second source of truth.

The stored field is vestigial — `core/render.py` derives the catalog's
`variants` from `tags` instead of reading it. It stays parsed and byte-
preserved on rewrite until ocx stops writing it and the keys are swept; the
round-trip tests below are the guard on that, since a root whose bytes
changed would open a pull request per package on its next announce.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from indexbot.core.observe import Observation
from indexbot.core.regenerate import regenerate
from indexbot.core.render import SourcePackage, build_render_plan
from indexbot.core.validate_entry import (
    check_variants_match_tags,
    parse_package_root,
    serialize_package_root,
)
from indexbot.core.version_order import variant_names
from indexbot.errors import ValidationError
from indexbot.model import Owner, PackageId, PackageRoot, TagEntry
from tests.fakes import FixedClock

_OWNER = Owner(github="alice", github_id=1)
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64

_INDEX_RAW = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "1" * 64,
                "size": 512,
                "platform": {"architecture": "amd64", "os": "linux"},
            }
        ],
    }
).encode()


def _root(tags: dict[str, TagEntry], *, variants: tuple[str, ...] = ()) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/astral-sh/python-build-standalone",
        repository="oci://ghcr.io/ocx-contrib/python-build-standalone",
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-07-29",
        desc=None,
        variants=variants,
        tags=dict(tags),
    )


def _observation(tag: str, digest: str) -> Observation:
    return Observation(tag=tag, content_digest=digest, raw=_INDEX_RAW, source=None)


def _entry(digest: str) -> TagEntry:
    return TagEntry(content=digest, observed="T0")


# --- derivation ------------------------------------------------------------


def test_regenerate_records_the_observed_variant_set() -> None:
    result = regenerate(
        _root({}),
        (_observation("3.13.1", _DIGEST_A), _observation("slim-3.13.1", _DIGEST_B)),
        None,
        FixedClock(fixed="T1"),
    )
    assert result.variants == ("slim",)


def test_regenerate_records_nothing_when_only_the_default_variant_ships() -> None:
    result = regenerate(
        _root({}),
        (_observation("3.13.1", _DIGEST_A), _observation("latest", _DIGEST_A)),
        None,
        FixedClock(fixed="T1"),
    )
    assert result.variants == ()


def test_regenerate_drops_a_variant_whose_last_tag_disappeared_upstream() -> None:
    # Carrying the field over verbatim would leave the root claiming a variant
    # its own `tags` no longer supports.
    current = _root(
        {"3.13.1": _entry(_DIGEST_A), "slim-3.13.1": _entry(_DIGEST_B)}, variants=("slim",)
    )
    result = regenerate(current, (_observation("3.13.1", _DIGEST_A),), None, FixedClock(fixed="T1"))
    assert result.variants == ()


def test_recorded_variants_match_the_derivation_over_the_recorded_tags() -> None:
    # The invariant that makes the field safe to read instead of re-derive:
    # whatever a consumer computes from `tags` is what the root already says.
    observations = (
        _observation("3.13.1", _DIGEST_A),
        _observation("slim-3.13.1", _DIGEST_B),
        _observation("slim-3.13", _DIGEST_B),
        _observation("musl-3.13.1-rc1", _DIGEST_A),
        _observation("latest", _DIGEST_A),
        _observation("nightly", _DIGEST_A),
    )
    result = regenerate(_root({}), observations, None, FixedClock(fixed="T1"))
    assert result.variants == variant_names(result.tags)
    assert result.variants == ("musl", "slim")


def test_regenerate_is_idempotent_for_variants() -> None:
    observations = (_observation("3.13.1", _DIGEST_A), _observation("slim-3.13.1", _DIGEST_B))
    first = regenerate(_root({}), observations, None, FixedClock(fixed="T1"))
    second = regenerate(first, observations, None, FixedClock(fixed="T2"))
    assert serialize_package_root(second) == serialize_package_root(first)


# --- wire spelling ---------------------------------------------------------


def test_variants_serializes_immediately_before_tags() -> None:
    # CONTRACTS.md §14 keeps `tags` last; a publisher's own port
    # (ocx `package announce`) writes the same slot, and the byte gate
    # rejects any other position.
    raw = serialize_package_root(_root({"3.13.1": _entry(_DIGEST_A)}, variants=("slim",)))
    keys = list(json.loads(raw).keys())
    assert keys[-2:] == ["variants", "tags"]


def test_an_empty_variant_set_omits_the_key_entirely() -> None:
    # Not `"variants": []`. One state, one spelling — and it is what keeps
    # every root published before this field existed byte-identical, so no
    # announce rewrites them and opens a pull request per package.
    raw = serialize_package_root(_root({"3.13.1": _entry(_DIGEST_A)}))
    assert b"variants" not in raw


def test_a_root_without_variants_round_trips_byte_identically() -> None:
    raw = serialize_package_root(_root({"3.13.1": _entry(_DIGEST_A)}))
    assert serialize_package_root(parse_package_root(raw)) == raw


def test_a_root_with_variants_round_trips_byte_identically() -> None:
    raw = serialize_package_root(_root({"slim-3.13.1": _entry(_DIGEST_B)}, variants=("slim",)))
    assert serialize_package_root(parse_package_root(raw)) == raw
    assert parse_package_root(raw).variants == ("slim",)


def test_parsing_a_root_that_predates_the_field_yields_an_empty_tuple() -> None:
    legacy = (
        json.dumps(
            {
                "name": "ocx.sh/astral-sh/python-build-standalone",
                "repository": "oci://ghcr.io/ocx-contrib/python-build-standalone",
                "owners": [{"github": "alice", "github_id": 1}],
                "status": "active",
                "deprecated_message": None,
                "created": "2026-07-29",
                "desc": None,
                "tags": {"3.13.1": {"content": _DIGEST_A, "observed": "T0"}},
            },
            indent=2,
        ).encode()
        + b"\n"
    )
    assert parse_package_root(legacy).variants == ()
    assert serialize_package_root(parse_package_root(legacy)) == legacy


# --- the projection is enforced, not merely described -----------------------


def test_a_root_whose_variants_match_its_tags_passes_the_check() -> None:
    check_variants_match_tags(_root({"slim-3.13.1": _entry(_DIGEST_B)}, variants=("slim",)))


def test_a_root_predating_the_field_passes_the_check() -> None:
    # Every root published before `variants` existed: no field, no variant
    # tags, so the derivation and the (empty) recorded set agree trivially.
    check_variants_match_tags(_root({"3.13.1": _entry(_DIGEST_A)}))


def test_a_variant_the_tags_do_not_support_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        check_variants_match_tags(_root({"3.13.1": _entry(_DIGEST_A)}, variants=("slim",)))
    assert "slim" in str(excinfo.value)


def test_a_present_field_that_omits_a_variant_its_tags_imply_is_rejected() -> None:
    # The under-claiming direction. Every other rejection test here is a root
    # claiming MORE than its tags support, so relaxing the check to
    # `set(root.variants) <= set(derived)` — accept any subset — passes the
    # whole module. Absence asserts nothing and is legal; a *present* field is
    # a claim about the whole set and is held to it in both directions.
    with pytest.raises(ValidationError) as excinfo:
        check_variants_match_tags(
            _root(
                {"slim-3.13.1": _entry(_DIGEST_B), "musl-3.13.1": _entry(_DIGEST_A)},
                variants=("slim",),
            )
        )
    assert "musl" in str(excinfo.value)


def test_a_root_storing_no_variants_is_accepted_even_when_its_tags_imply_some() -> None:
    # The stage-2 shape: once ocx stops writing the field, this is what every
    # announce from a variant-shipping package looks like. Demanding presence
    # would red all of them. An absent field asserts nothing, so there is
    # nothing to tamper with, and the catalog derives the truth anyway.
    check_variants_match_tags(_root({"slim-3.13.1": _entry(_DIGEST_B)}))


def test_the_check_is_the_gate_a_hand_authored_root_cannot_pass() -> None:
    # The byte gate is a parse->serialize round-trip, so it accepts any
    # well-formed `variants` value; the schema cannot express "matches the
    # tags" either. Without this check a hand-authored PR could claim a
    # variant set no derivation could produce, and it would survive until the
    # next announce silently overwrote it.
    hand_authored = _root({"3.13.1": _entry(_DIGEST_A)}, variants=("nonexistent",))
    assert serialize_package_root(parse_package_root(serialize_package_root(hand_authored))) == (
        serialize_package_root(hand_authored)
    ), "the byte gate passes it"
    with pytest.raises(ValidationError):
        check_variants_match_tags(hand_authored)


# --- catalog view-model ----------------------------------------------------


def _catalog_row(root: PackageRoot) -> dict[str, object]:
    source = SourcePackage(
        package_id=PackageId(namespace="astral-sh", package="python-build-standalone"),
        root=root,
        root_raw=serialize_package_root(root),
        # Every tag's `content` must resolve to a committed CAS object or the
        # render plan refuses the package outright.
        content_by_digest={f"{entry.content}.json": _INDEX_RAW for entry in root.tags.values()},
    )
    plan = build_render_plan((source,))
    catalog = next(f for f in plan if f.path == "data/catalog/catalog.json")
    document = cast("dict[str, Any]", json.loads(catalog.content))
    packages = cast("list[dict[str, object]]", document["packages"])
    assert len(packages) == 1
    return packages[0]


def test_the_catalog_grid_row_carries_the_variants_the_tags_imply() -> None:
    # The grid's `latestVersion` deliberately skips variant-prefixed tags, so
    # without this key a browsing user cannot tell a package that ships
    # variants from one that does not.
    row = _catalog_row(_root({"slim-3.13.1": _entry(_DIGEST_B)}, variants=("slim",)))
    assert row["variants"] == ["slim"]


def test_the_catalog_grid_row_omits_variants_when_there_are_none() -> None:
    row = _catalog_row(_root({"3.13.1": _entry(_DIGEST_A)}))
    assert "variants" not in row


def test_the_catalog_row_derives_variants_rather_than_reading_the_root() -> None:
    # The discriminating test. A root whose recorded field disagrees with its
    # own tags is not a state the bot can produce (`regenerate` re-derives and
    # `check_variants_match_tags` rejects it), but it is the only way to tell
    # "parse the tags" from "read the field" — and the field is vestigial.
    row = _catalog_row(_root({"slim-3.13.1": _entry(_DIGEST_B)}, variants=("musl",)))
    assert row["variants"] == ["slim"]


def test_the_catalog_row_derives_variants_for_a_root_that_stores_none() -> None:
    # Once ocx stops writing the field (stage 2) every announced root looks
    # like this. Before the derivation it rendered no `variants` key at all.
    row = _catalog_row(_root({"3.13.1": _entry(_DIGEST_A), "slim-3.13.1": _entry(_DIGEST_B)}))
    assert row["variants"] == ["slim"]
