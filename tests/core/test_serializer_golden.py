"""Golden fixture tests for `core/validate_entry.py`'s byte-exact serializers
(CONTRACTS.md §14).

Every fixture under `tests/golden/serializer/` is produced by calling the
real `serialize_package_root`/`serialize_observation_object` functions and
committing their exact output bytes — never hand-typed JSON (see
`tests/golden/serializer/README.md` for the generation procedure). These
tests prove the committed bytes still round-trip through
parse -> serialize byte-for-byte, so any drift in field order, indentation,
or ASCII-escaping is caught immediately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from indexbot.core.validate_entry import (
    check_digest_self_consistent,
    parse_observation_object,
    parse_package_root,
    serialize_observation_object,
    serialize_package_root,
)

_GOLDEN_ROOT = Path(__file__).parent.parent / "golden" / "serializer"

_ROOT_FIXTURES = sorted((_GOLDEN_ROOT / "root").glob("*.json"))
_OBSERVATION_FIXTURES = sorted((_GOLDEN_ROOT / "observation" / "sha256").glob("*.json"))


@pytest.mark.parametrize("fixture_path", _ROOT_FIXTURES, ids=lambda p: p.name)
def test_root_fixture_round_trips(fixture_path: Path) -> None:
    raw = fixture_path.read_bytes()
    parsed = parse_package_root(raw)
    assert serialize_package_root(parsed) == raw


@pytest.mark.parametrize("fixture_path", _OBSERVATION_FIXTURES, ids=lambda p: p.name)
def test_observation_fixture_round_trips(fixture_path: Path) -> None:
    raw = fixture_path.read_bytes()
    parsed = parse_observation_object(raw)
    assert serialize_observation_object(parsed) == raw


@pytest.mark.parametrize("fixture_path", _OBSERVATION_FIXTURES, ids=lambda p: p.name)
def test_observation_fixture_digest_self_consistent(fixture_path: Path) -> None:
    raw = fixture_path.read_bytes()
    check_digest_self_consistent(f"sha256:{fixture_path.stem}", raw)


def test_required_fixture_inventory() -> None:
    """The round-trip tests above glob the fixture dirs, so a rename or
    packaging error that empties a glob makes them vacuously pass (pytest skips
    an empty parametrization). Fail loudly instead if the register §3 minimum
    set — the two named roots and at least one observation vector — goes missing.
    """
    root_names = {p.name for p in _ROOT_FIXTURES}
    assert {"minimal.json", "full-fields.json"} <= root_names, (
        f"missing required root fixtures; found {sorted(root_names)}"
    )
    assert _OBSERVATION_FIXTURES, "no observation fixtures found"
