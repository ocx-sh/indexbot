# Golden serializer fixtures

Byte-exact committed vectors for `core/validate_entry.py`'s
`serialize_package_root`/`serialize_observation_object` (CONTRACTS.md §14).
Consumed by `tests/core/test_serializer_golden.py` (round-trip gate, rides
`task bot:test`) and vendored verbatim into `ocx-sh/ocx`
(`crates/ocx_lib/tests/fixtures/index_wire/`) as the Rust client's own
conformance vectors.

## Layout

- `root/*.json` — `PackageRoot` fixtures, the human-diffable pretty-printed
  form (`serialize_package_root`'s exact output — 2-space indent, insertion
  order, trailing newline).
- `observation/sha256/<hex>.json` — `ObservationObject` fixtures, §1's
  canonical minified form (`serialize_observation_object`'s exact output —
  no whitespace, alphabetized keys, no trailing newline). `<hex>` is the
  sha256 hex digest of the fixture's own bytes, matching the real CAS
  filename convention.

## Never hand-edit

Every byte in this directory is the literal return value of calling the real
serializer function on a constructed `model.PackageRoot`/
`model.ObservationObject` instance — never hand-typed JSON. Editing a fixture
directly (even to "fix" formatting) defeats the entire point of a byte-exact
gate; `tests/core/test_serializer_golden.py`'s round-trip assertion will
catch it, but the fixture is also then lying about being real serializer
output. Regenerate via the procedure below instead.

## Regeneration procedure

1. Write an uncommitted scratch script (never commit it) that imports
   `indexbot.model` and `indexbot.core.validate_entry`, constructs the
   dataclass instance(s) you want to exercise, and calls
   `serialize_package_root`/`serialize_observation_object` on them.
2. Write the returned `bytes` to the fixture file verbatim
   (`Path(...).write_bytes(...)`) — never `str`/`json.dumps` a second time.
3. For an observation fixture, name the file
   `hashlib.sha256(returned_bytes).hexdigest() + ".json"` — the filename's
   hex stem must equal the fixture's own content digest
   (`test_observation_fixture_digest_self_consistent` enforces this via
   `check_digest_self_consistent`).
4. Run `uv run pytest tests/core/test_serializer_golden.py -v` and confirm
   the new fixture round-trips.
5. Delete the scratch script before committing.

This is the exact procedure used to generate every fixture currently in this
directory (`minimal.json`/`full-fields.json` exercise the root serializer's
omit-when-`None` fields, the `desc: null` vs. omitted-key distinction, a
yanked tag, and a non-ASCII `desc.title` — `\uXXXX`-escape coverage). The two
observation vectors exercise the observation serializer independently of the
root one: the multi-platform vector covers the tuple-based `platform_sort_key`
(two `linux/amd64` platforms differing only in `os_features`, libc.glibc vs.
libc.musl), and the second carries a non-ASCII `os_features` token so the
observation path's own `ensure_ascii` `\uXXXX` escaping is pinned across the
Python bot and the Rust client (it shares the idiom with the root serializer
but is a separate call — the root fixture alone would not catch a divergence).
