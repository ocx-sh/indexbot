"""Golden fixture tests for `tests/golden/`'s committed byte vectors.

Two directories, two different claims:

- `serializer/root/*.json` is produced by calling the real
  `serialize_package_root` and committing its exact output bytes — never
  hand-typed JSON (see `tests/golden/serializer/README.md`). The round-trip
  assertion below proves those bytes still survive parse -> serialize
  byte-for-byte, so any drift in field order, indentation, or ASCII-escaping
  is caught immediately.
- `dispatch/sha256/<hex>.json` is a registry's own OCI image index, stored
  verbatim under the CAS convention `<hex> == sha256(file bytes)` that
  `p/<ns>/<pkg>/o/sha256/<hex>.json` uses in a real index tree. Nothing
  re-serializes these; the claim they carry is that CAS invariant, asserted
  below — this is the repository's only golden-level "filename hex ==
  sha256(bytes)" check.

The dispatch vectors are captured registry bodies, not any encoder's
canonical output: they carry the publisher's own key order, which
`json.dumps(..., sort_keys=True)` would rewrite for all four. So a pipeline
that re-encoded them instead of copying their bytes could not reproduce these
filenames, and the invariant below is genuinely falsifiable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indexbot.core.validate_entry import (
    check_digest_self_consistent,
    parse_package_root,
    serialize_package_root,
)

_GOLDEN_ROOT = Path(__file__).parent.parent / "golden"
_DISPATCH_ROOT = _GOLDEN_ROOT / "dispatch"

_ROOT_FIXTURES = sorted((_GOLDEN_ROOT / "serializer" / "root").glob("*.json"))
_DISPATCH_FIXTURES = sorted((_DISPATCH_ROOT / "sha256").glob("*.json"))


@pytest.mark.parametrize("fixture_path", _ROOT_FIXTURES, ids=lambda p: p.name)
def test_root_fixture_round_trips(fixture_path: Path) -> None:
    raw = fixture_path.read_bytes()
    parsed = parse_package_root(raw)
    assert serialize_package_root(parsed) == raw


@pytest.mark.parametrize("fixture_path", _DISPATCH_FIXTURES, ids=lambda p: p.name)
def test_dispatch_fixture_digest_self_consistent(fixture_path: Path) -> None:
    raw = fixture_path.read_bytes()
    check_digest_self_consistent(f"sha256:{fixture_path.stem}", raw)


@pytest.mark.parametrize("fixture_path", _DISPATCH_FIXTURES, ids=lambda p: p.name)
def test_dispatch_fixture_is_not_canonical_json(fixture_path: Path) -> None:
    """Guards the check above from decaying into a tautology. If a future
    recapture happened to land on canonically-encoded bytes, a re-serializing
    pipeline would satisfy `test_dispatch_fixture_digest_self_consistent`
    by accident and the CAS invariant would stop pinning anything.
    """
    raw = fixture_path.read_bytes()
    canonical = json.dumps(
        json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert canonical != raw


def test_required_fixture_inventory() -> None:
    """The parametrized tests above glob their fixture dirs, so a rename or
    packaging error that empties a glob makes them vacuously pass (pytest skips
    an empty parametrization). Fail loudly instead if the register §3 minimum
    set — the two named roots — goes missing, and hold the dispatch directory
    to exactly the vector set `expected_platforms.json` declares, so neither an
    unlisted file nor a missing one slips past the CAS check above.
    """
    root_names = {p.name for p in _ROOT_FIXTURES}
    assert {"minimal.json", "full-fields.json"} <= root_names, (
        f"missing required root fixtures; found {sorted(root_names)}"
    )

    declared = json.loads((_DISPATCH_ROOT / "expected_platforms.json").read_bytes())
    declared_files = {vector["file"] for vector in declared["vectors"]}
    present_files = {p.relative_to(_DISPATCH_ROOT).as_posix() for p in _DISPATCH_FIXTURES}
    assert declared_files == present_files
