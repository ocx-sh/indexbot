"""Pure render pipeline (ADR-3, WP2-F) — reachability-filtered copy from the
committed `p/` source tree into two output trees, per ADR-3's fixed build
order:

1. `wrapper_pages` — VitePress compile *input*, written to `site/src/**`
   *before* `bun run docs:build` runs.
2. `dist_files` — `config.json`, the `/p/**` wire mirror, `/c/index.json`
   (bare package listing, CONTRACTS.md §8), and `/data/catalog/**` (catalog
   UI summary data, NOT wire contract — CONTRACTS.md §8), written to
   `site/.vitepress/dist/**` *after* the VitePress build (`emptyOutDir`
   footgun; see ADR-3 Technical Details).

`build_render_plan` is pure (CONTRACTS.md §0): no I/O, no ports. `cli/
render.py` (WP2-M) does the `FilePort` reads that assemble `SourcePackage`
and the writes that apply the returned `RenderPlan`.

The `/data/catalog/**` shape below is this module's own call (explicitly not
wire contract, ADR-3) — the `site/` slice has not landed on this branch yet
(parallel work), so its exact consumption shape is unconfirmed; see
`open_questions`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from indexbot.core.catalog_md import cas_relpath, render_wrapper_page
from indexbot.core.version_order import find_latest_version

if TYPE_CHECKING:
    from collections.abc import Sequence

    from indexbot.model import PackageId, PackageRoot


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
    """`path` is relative to whichever output root the containing
    `RenderPlan` field targets."""

    path: str
    content: str | bytes


@dataclass(frozen=True, slots=True)
class RenderPlan:
    wrapper_pages: tuple[FileWrite, ...]
    """Write BEFORE the VitePress build — target `site/src/**`."""

    dist_files: tuple[FileWrite, ...]
    """Write AFTER the VitePress build — target `site/.vitepress/dist/**`."""


def _reachable_digests(root: PackageRoot) -> frozenset[str]:
    """Content digests this package's CAS copy must keep (ADR-1 D8).

    Every *live* (non-yanked) tag's `content` digest, plus `desc.readme`/
    `desc.logo` (never yankable themselves). A yanked tag's content survives
    only incidentally, if some other live tag shares the same digest
    (emergent aliasing, ADR-1 D3, applies to reachability too) — CONTRACTS.md
    §8's explicit disambiguation of ADR-1 D8's "orphaned by a repointed or
    yanked tag" wording.
    """
    digests = {entry.content for entry in root.tags.values() if entry.yanked is None}
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


def _cas_ext_lookup(content_by_digest: dict[str, bytes]) -> dict[str, str]:
    """`digest -> ext`, from this package's actually-discovered CAS blob keys."""
    return dict(_split_content_key(key) for key in content_by_digest)


def _cas_url(
    namespace: str, package: str, digest: str | None, ext_lookup: dict[str, str]
) -> str | None:
    """Absolute CAS URL for `digest` (`desc.readme`/`desc.logo`), or `None`
    if no digest is configured (`desc is None`, or `desc.readme`/`.logo` is
    `None`). A configured digest missing from `ext_lookup` is a dangling
    reference — `core/validate_entry.py`'s `check_no_dangling_references`
    (upstream of render, G-15) is the layer responsible for catching that;
    render trusts its input and lets `KeyError` propagate as an unhandled
    bug rather than silently degrading the catalog entry.
    """
    if digest is None:
        return None
    return "/" + cas_relpath(namespace, package, digest, ext_lookup[digest])


def _catalog_entry(source: SourcePackage) -> dict[str, object]:
    """One `/data/catalog/packages.json` row — summary only, CAS URL refs for
    logo/readme rather than duplicated blob bytes (ADR-3's explicit
    divergence from `ocx-sh/ocx`'s website). Yanked tags are excluded (plan
    Phase 2 WP-list)."""
    namespace, package = source.package_id.namespace, source.package_id.package
    root = source.root
    desc = root.desc
    live_tag_count = sum(1 for entry in root.tags.values() if entry.yanked is None)
    ext_lookup = _cas_ext_lookup(source.content_by_digest)
    logo_digest = desc.logo if desc is not None else None
    readme_digest = desc.readme if desc is not None else None

    return {
        "namespace": namespace,
        "package": package,
        "name": root.name,
        "status": root.status,
        "title": desc.title if desc is not None else root.name,
        "description": desc.description if desc is not None else "",
        "keywords": list(desc.keywords) if desc is not None else [],
        "latestVersion": find_latest_version(root.tags),
        "tagCount": live_tag_count,
        "logoUrl": _cas_url(namespace, package, logo_digest, ext_lookup),
        "readmeUrl": _cas_url(namespace, package, readme_digest, ext_lookup),
    }


def _package_index(ordered: Sequence[SourcePackage], *, format_version: int) -> str:
    """`c/index.json` — a bare package listing: every package id in
    `ordered` mapped to its root's content digest (CONTRACTS.md §8). The
    digest sources from `source.root_raw`'s exact committed bytes, never a
    re-serialization through the dataclass — the same "root bytes are never
    digested for wire-contract purposes, only referenced" rationale as
    `validate_entry.serialize_package_root`; here it's simply hashed for a
    listing digest, not a CAS content address."""
    packages = {
        str(source.package_id): f"sha256:{hashlib.sha256(source.root_raw).hexdigest()}"
        for source in ordered
    }
    return json.dumps({"format_version": format_version, "packages": packages}, indent=2) + "\n"


def build_render_plan(packages: Sequence[SourcePackage], *, format_version: int = 1) -> RenderPlan:
    """Pure (CONTRACTS.md §0) — no I/O. See module docstring for the two
    output trees and their write-order contract."""
    ordered = sorted(packages, key=lambda source: str(source.package_id))

    wrapper_pages = tuple(
        FileWrite(
            path=f"{source.package_id.namespace}/{source.package_id.package}.md",
            content=render_wrapper_page(source.root),
        )
        for source in ordered
    )

    dist_files: list[FileWrite] = [
        FileWrite(
            path="config.json",
            content=json.dumps({"format_version": format_version}, indent=2) + "\n",
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

    dist_files.append(
        FileWrite(
            path="data/catalog/packages.json",
            content=json.dumps([_catalog_entry(source) for source in ordered], indent=2) + "\n",
        )
    )

    return RenderPlan(wrapper_pages=wrapper_pages, dist_files=tuple(dist_files))
