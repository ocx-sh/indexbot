# Golden serializer fixtures

Byte-exact committed vectors for `core/validate_entry.py`'s
`serialize_package_root` (CONTRACTS.md §14). Consumed by
`tests/core/test_serializer_golden.py`'s round-trip gate, which rides
`task bot:test`.

## Vendoring scope

`root/*.json` is vendored verbatim into `ocx-sh/ocx`
(`crates/ocx_lib/tests/fixtures/index_wire/`) as the Rust client's own
conformance vectors: the client re-serializes a package root, so its output
must match these bytes exactly.

The sibling `../tag_verdicts.json` and `../dispatch/**` are vendored under the
same `SOURCE_COMMIT` pin but carry **no** byte-exactness claim. They are not
serializer output and nothing re-serializes them: `tag_verdicts.json` is a
decision table (tag name -> reserved verdict), and `dispatch/**` holds
registries' own OCI image indices, read for their decoded content and for the
CAS invariant `<hex> == sha256(file bytes)`.

## Layout

- `root/*.json` — `PackageRoot` fixtures, the human-diffable pretty-printed
  form (`serialize_package_root`'s exact output — 2-space indent, insertion
  order, trailing newline).

## Never hand-edit

Every byte in this directory is the literal return value of calling the real
serializer function on a constructed `model.PackageRoot` instance — never
hand-typed JSON. Editing a fixture directly (even to "fix" formatting) defeats
the entire point of a byte-exact gate; `tests/core/test_serializer_golden.py`'s
round-trip assertion will catch it, but the fixture is also then lying about
being real serializer output. Regenerate via the procedure below instead.

## Regeneration procedure

1. Write an uncommitted scratch script (never commit it) that imports
   `indexbot.model` and `indexbot.core.validate_entry`, constructs the
   `PackageRoot` instance(s) you want to exercise, and calls
   `serialize_package_root` on them.
2. Write the returned `bytes` to the fixture file verbatim
   (`Path(...).write_bytes(...)`) — never `str`/`json.dumps` a second time.
3. Run `uv run pytest tests/core/test_serializer_golden.py -v` and confirm
   the new fixture round-trips.
4. Delete the scratch script before committing.

This is the exact procedure used to generate every fixture currently in this
directory. `minimal.json`/`full-fields.json` exercise the root serializer's
omit-when-`None` fields, the `desc: null` vs. omitted-key distinction, a
yanked tag, and a non-ASCII `desc.title` — `\uXXXX`-escape coverage;
`with-source.json` adds the optional `source` field, positioned between
`superseded_by` and `tags`, alongside an `upstream.repository_url` naming a
*different* repository — the mirror-vs-vendor distinction the two fields
carry.
