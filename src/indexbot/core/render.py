"""Pure render pipeline (ADR-3, WP2-F; reshaped by plan_site_redesign
WP-bot, then by the `@ocx-sh/catalog` extraction, `plan_catalog_extraction`
WP-11) — reachability-filtered copy from the committed `p/` source tree
into one output tree (`dist_files`): `config.json` and the `/p/**` wire
mirror + CAS, plus `/c/index.json` (bare package listing, CONTRACTS.md §8),
written to `site/.vitepress/dist/**` *after* the VitePress build
(`emptyOutDir` footgun; see ADR-3 Technical Details).

The site redesign (plan_site_redesign) retired this module's other output
tree — per-package VitePress wrapper Markdown (`wrapper_pages`, `site/src/
**`) — in favor of dynamic routes that glob `p/*/*.json` directly at
VitePress build time; this module now only ever emits `dist_files`, so
`build_render_plan` returns that flat `tuple[FileWrite, ...]` rather than a
two-tree `RenderPlan` wrapper.

`build_render_plan` is pure (CONTRACTS.md §0): no I/O, no ports. `cli/
render.py` (WP2-M) does the `FilePort` reads that assemble `SourcePackage`
and the writes that apply the returned files.

The catalog-grid view-model this module used to emit at
`/data/catalog/catalog.json` (never wire contract, ADR-3) is **retired** —
that projection now lives entirely in the `@ocx-sh/catalog` npm package's
own view-model emitter (`cat/src/viewmodel/`, a byte-gated TS port of this
module's former `_catalog_platforms`/`_latest_activity`/`_catalog_entry`/
`_generated_timestamp`/`_catalog_index` functions), which reads the wire
tree this module still produces and renders the catalog UI around it. See
CONTRACTS.md §8.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from indexbot.core.validate_entry import cas_relpath

if TYPE_CHECKING:
    from collections.abc import Sequence

    from indexbot.model import PackageId, PackageRoot


NAME_SEGMENTS = 2
"""Segment count a package name has under this index, counted on the part
*after* `ocx.sh/` — published in `config.json` so a client knows the shape of
name this deployment can hold without having to ask.

It restates `schema/root.schema.json`'s `^ocx\\.sh/<ns>/<pkg>$` pattern; the
two must move together. A client that reads it resolves a flat name like
`ocx.sh/go-task` as plain OCI instead of reading the unavoidable 404 as an
authoritative refusal. Optional on the wire and purely additive — a client
that predates the field ignores it and behaves exactly as before, which is
also why it is not a security control."""


@dataclass(frozen=True, slots=True)
class SourcePackage:
    """One package's fully-loaded source-tree state — `cli/render.py`'s input
    unit, assembled via `FilePort` reads (`list_files` over `p/`, `read_text`
    per root, `read_bytes` per CAS object)."""

    package_id: PackageId
    root: PackageRoot
    root_raw: bytes
    content_by_digest: dict[str, bytes]
    """Key = `f"{digest}.{ext}"` (e.g. `"sha256:<hex>.json"`, `.md`, `.svg`,
    `.png`) — a CAS digest alone does not carry its extension, only the
    filename `cli/render.py` discovers via `FilePort.list_files` does
    (CONTRACTS.md §8). Every CAS blob under this package's `o/sha256/` tree,
    unfiltered — `build_render_plan` applies the reachability filter."""


@dataclass(frozen=True, slots=True)
class FileWrite:
    """`path` is relative to the dist output root (`--out`)."""

    path: str
    content: str | bytes


def _live_tag_content_digests(root: PackageRoot) -> frozenset[str]:
    """Content digests of every *live* (non-yanked) tag (ADR-1 D8) — this
    module's own CAS-pruning input (`_reachable_digests`). The catalog
    package's view-model emitter ports the same digest-iteration rule
    independently for its platform union (`cat/src/viewmodel/`, WP-05) —
    this module no longer has a second in-tree caller of its own."""
    return frozenset(entry.content for entry in root.tags.values() if entry.yanked is None)


def _reachable_digests(root: PackageRoot) -> frozenset[str]:
    """Content digests this package's CAS copy must keep (ADR-1 D8).

    Every *live* (non-yanked) tag's `content` digest, plus `desc.readme`/
    `desc.logo` (never yankable themselves). A yanked tag's content survives
    only incidentally, if some other live tag shares the same digest
    (emergent aliasing, ADR-1 D3, applies to reachability too) — CONTRACTS.md
    §8's explicit disambiguation of ADR-1 D8's "orphaned by a repointed or
    yanked tag" wording.
    """
    digests = set(_live_tag_content_digests(root))
    if root.desc is not None:
        if root.desc.readme is not None:
            digests.add(root.desc.readme)
        if root.desc.logo is not None:
            digests.add(root.desc.logo)
    return frozenset(digests)


def _split_content_key(key: str) -> tuple[str, str]:
    """`"sha256:<hex>.<ext>"` -> `("sha256:<hex>", "<ext>")`."""
    digest, _, ext = key.rpartition(".")
    return digest, ext


def _package_dist_files(source: SourcePackage) -> list[FileWrite]:
    namespace, package = source.package_id.namespace, source.package_id.package
    files = [FileWrite(path=f"p/{namespace}/{package}.json", content=source.root_raw)]

    reachable = _reachable_digests(source.root)
    for key, content in source.content_by_digest.items():
        digest, ext = _split_content_key(key)
        if digest in reachable:
            path = cas_relpath(namespace, package, digest, ext)
            files.append(FileWrite(path=path, content=content))
    return files


def _package_index(ordered: Sequence[SourcePackage], *, format_version: int) -> str:
    """`c/index.json` — a versioned package listing: `format_version` beside a
    `packages` map of every package id in `ordered` to its root's content
    digest (CONTRACTS.md §8). The envelope, not a bare map: the version pin
    travels with the listing exactly as it does in `config.json`, so a client
    reading a catalog knows which grammar produced it without a second fetch.

    The digest sources from `source.root_raw`'s exact committed bytes, never a
    re-serialization through the dataclass — the same "root bytes are never
    digested for wire-contract purposes, only referenced" rationale as
    `validate_entry.serialize_package_root`; here it's simply hashed for a
    listing digest, not a CAS content address."""
    packages = {
        str(source.package_id): f"sha256:{hashlib.sha256(source.root_raw).hexdigest()}"
        for source in ordered
    }
    return json.dumps({"format_version": format_version, "packages": packages}, indent=2) + "\n"


def build_render_plan(
    packages: Sequence[SourcePackage], *, format_version: int = 1
) -> tuple[FileWrite, ...]:
    """Pure (CONTRACTS.md §0) — no I/O. Returns the flat dist-tree file list;
    see module docstring for its shape and write-order contract
    (`site:build` before this tree lands, `--out`)."""
    ordered = sorted(packages, key=lambda source: str(source.package_id))

    dist_files: list[FileWrite] = [
        FileWrite(
            path="config.json",
            content=json.dumps(
                {"format_version": format_version, "name_segments": NAME_SEGMENTS},
                indent=2,
            )
            + "\n",
        )
    ]
    for source in ordered:
        dist_files.extend(_package_dist_files(source))

    dist_files.append(
        FileWrite(
            path="c/index.json",
            content=_package_index(ordered, format_version=format_version),
        )
    )

    return tuple(dist_files)
