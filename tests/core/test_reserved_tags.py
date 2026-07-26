"""D7's reserved-tag rule, pinned row by row.

`is_reserved_tag` is one rule with three implementations across two repos —
this predicate, `schema/root.schema.json`'s `propertyNames.not`, and the OCX
client's `Tag::Canonical`. The shared verdict table is what keeps them
together; a row removed or a verdict flipped without the implementation
moving must fail here.

`_CASES` mirrors the shape of `bot/tests/golden/tag_verdicts.json` (WP-B0a,
not yet in this tree): `{tag, reserved, why}`, `why` documentation only,
never asserted. When that fixture lands, this list is replaced by a
`json.loads` of it and nothing else in this file changes.
"""

from __future__ import annotations

import pytest

from indexbot.core.observe import observe
from indexbot.core.validate_entry import check_no_reserved_tags, is_reserved_tag
from indexbot.errors import ValidationError
from indexbot.model import Owner, PackageRoot, TagEntry
from tests.fakes import FakeRegistry

_HEX_64 = "a" * 64
_HEX_96 = "b" * 96
_HEX_128 = "c" * 128

_CASES: list[dict[str, object]] = [
    {"tag": "1.0.0", "reserved": False, "why": "an ordinary version tag"},
    {"tag": "latest", "reserved": False, "why": "the canonical floating tag"},
    {"tag": "v2.3.4-rc.1", "reserved": False, "why": "prerelease punctuation is not reserved"},
    {"tag": "__ocx.desc", "reserved": True, "why": "OCX's description tag"},
    {"tag": "__ocx", "reserved": True, "why": "the prefix alone; no dot is required by D7"},
    {"tag": "__ocxfoo", "reserved": True, "why": "reserved by prefix, not by exact name"},
    {"tag": "__OCX.desc", "reserved": True, "why": "the prefix is case-insensitive"},
    {"tag": "__oc", "reserved": False, "why": "boundary: shorter than the prefix"},
    {"tag": "x__ocx", "reserved": False, "why": "boundary: contains, does not start with"},
    {"tag": f"sha256.{_HEX_64}", "reserved": True, "why": "the canonical tag ocx push writes"},
    {"tag": f"sha256.{_HEX_64.upper()}", "reserved": True, "why": "hex is case-insensitive"},
    {
        "tag": f"sha256.{'a' * 63}",
        "reserved": False,
        "why": "boundary: 63 hex digits is not a sha256 digest",
    },
    {
        "tag": f"sha256.{'z' * 64}",
        "reserved": False,
        "why": "boundary: right length, not hex — catches a starts_with check",
    },
    {"tag": "sha256", "reserved": False, "why": "boundary: algorithm name with no digest"},
    {
        "tag": f"sha384.{_HEX_96}",
        "reserved": True,
        "why": "an implementation hardcoding sha256 passes every row but this one",
    },
    {"tag": f"sha512.{_HEX_128}", "reserved": True, "why": "same, for sha512"},
    {
        "tag": f"sha384.{_HEX_64}",
        "reserved": False,
        "why": "boundary: sha384 prefix, sha256's hex length — catches a prefix-only check",
    },
]

_IDS = [str(case["tag"]) for case in _CASES]


def test_the_case_table_is_the_frozen_shape() -> None:
    assert len(_CASES) == 17
    assert len(set(_IDS)) == 17
    for case in _CASES:
        assert set(case) == {"tag", "reserved", "why"}
    for algorithm in ("sha256.", "sha384.", "sha512."):
        assert any(
            case["reserved"] and str(case["tag"]).startswith(algorithm) for case in _CASES
        ), algorithm


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_reserved_tag_predicate_matches_the_shared_fixture(case: dict[str, object]) -> None:
    assert is_reserved_tag(str(case["tag"])) is case["reserved"], case["why"]


# --- the root-level gate (D7 at the index layer) ---------------------------


def _root(tags: dict[str, TagEntry]) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(Owner(github="alice", github_id=1),),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags),
    )


_ENTRY = TagEntry(content="sha256:" + "a" * 64, observed="2026-07-17T00:00:00Z")


@pytest.mark.parametrize(
    "tag",
    [
        "__ocx.desc",
        "__ocx",
        "__ocxfoo",
        "__OCX.desc",
        f"sha256.{_HEX_64}",
        f"sha384.{_HEX_96}",
        f"sha512.{_HEX_128}",
    ],
)
def test_check_no_reserved_tags_rejects_a_hand_authored_reserved_tag(tag: str) -> None:
    with pytest.raises(ValidationError, match="reserved tag name"):
        check_no_reserved_tags(_root({tag: _ENTRY}))


def test_check_no_reserved_tags_lists_every_offender() -> None:
    root = _root({"__ocx": _ENTRY, f"sha256.{_HEX_64}": _ENTRY, "1.0.0": _ENTRY})
    with pytest.raises(ValidationError) as excinfo:
        check_no_reserved_tags(root)
    assert "'__ocx'" in str(excinfo.value)
    assert f"'sha256.{_HEX_64}'" in str(excinfo.value)


def test_check_no_reserved_tags_admits_ordinary_tags() -> None:
    check_no_reserved_tags(_root({"1.2.3": _ENTRY, "latest": _ENTRY}))


# --- one rule, two callers: the sweep and the gate agree -------------------


def test_the_sweep_excludes_exactly_what_the_gate_rejects() -> None:
    """`observe()` skips a tag iff `check_no_reserved_tags` would reject it.
    Asserted against the same table, so the two callers cannot drift apart
    while each stays internally consistent."""
    repository = "oci://ghcr.io/ocx-contrib/cmake"
    index: dict[str, object] = {
        "manifests": [
            {"platform": {"architecture": "amd64", "os": "linux"}, "digest": "sha256:" + "1" * 64}
        ]
    }
    tags = [str(case["tag"]) for case in _CASES]
    registry = FakeRegistry(
        tags={repository: tags}, manifests={(repository, tag): index for tag in tags}
    )
    observed = {observation.tag for observation in observe(repository, registry)}
    assert observed == {str(case["tag"]) for case in _CASES if not case["reserved"]}
