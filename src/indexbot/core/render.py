"""Pure render pipeline (ADR-3, WP2-F; reshaped by plan_site_redesign
WP-bot) — reachability-filtered copy from the committed `p/` source tree
into one output tree (`dist_files`): `config.json`, the `/p/**` wire mirror
+ CAS, `/c/index.json` (bare package listing, CONTRACTS.md §8), and
`/data/catalog/catalog.json` (catalog-grid view-model, NOT wire contract —
CONTRACTS.md §8), written to `site/.vitepress/dist/**` *after* the VitePress
build (`emptyOutDir` footgun; see ADR-3 Technical Details).

The site redesign (plan_site_redesign) retired this module's other output
tree — per-package VitePress wrapper Markdown (`wrapper_pages`, `site/src/
**`) — in favor of dynamic routes that glob `p/*/*.json` directly at
VitePress build time; this module now only ever emits `dist_files`, so
`build_render_plan` returns that flat `tuple[FileWrite, ...]` rather than a
two-tree `RenderPlan` wrapper.

`build_render_plan` is pure (CONTRACTS.md §0): no I/O, no ports. `cli/
render.py` (WP2-M) does the `FilePort` reads that assemble `SourcePackage`
and the writes that apply the returned files.

The `/data/catalog/catalog.json` shape below is this module's own call
(explicitly not wire contract, ADR-3) — frozen by plan_site_redesign's
`/data/catalog/catalog.json` contract: `{"generated": <str|null>, "packages":
[...]}`, `generated` being a lexicographic (== chronological, fixed-shape
UTC timestamps) string max over every tag's `observed`/`yanked.at` value
across every package, `null` when no tag has ever been observed. No
`datetime` import anywhere in this module — a wall-clock `generated` would
break `render --check` idempotency.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from indexbot.core.validate_entry import cas_relpath
from indexbot.core.version_order import find_latest_version, variant_names

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
    """Content digests of every *live* (non-yanked) tag (ADR-1 D8) — shared
    by `_reachable_digests` (CAS pruning) and `_catalog_platforms` (platform
    union across the tags' image indices), this module's only two genuinely
    different consumers of the same live-tag digest iteration
    (quality-core.md DRY: extraction justified by 2+ real callers)."""
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


def _catalog_platforms(source: SourcePackage) -> list[str]:
    """`f"{os}/{architecture}"` union across every live tag's image index,
    deduped + sorted (frozen `/data/catalog/catalog.json` contract's
    `platforms` field).

    Each live tag's CAS bytes are the registry's OCI image index verbatim,
    so this reads `manifests[]` directly. A descriptor contributes a platform
    only when it names one: `platform.os` and `platform.architecture` both
    present as strings, `os` not `"unknown"` (ADR R4). Everything else is
    skipped — no `platform` key at all, a `platform` that is not an object,
    one missing or mistyping either field, and the `"os": "unknown"`
    attestation shape. These are the referrer/attestation descriptors a real
    registry serves alongside the runnable images; a platform matrix that
    listed them would advertise `unknown/unknown` as an installable target.

    This is the one read in this module that does *not* trust its input. The
    upstream gates (`cli/validate.py`'s image-index shape gate,
    `parse_image_index_digests`) check `manifests[]` down to each
    descriptor's `digest`, and stop there — `platform` is unvalidated
    publisher bytes all the way to here, so a missing `architecture` key
    would otherwise `KeyError` out of `build_render_plan`, which `cli/
    render.py` calls with every package in one list: one publisher, whole
    site render dead, and outside `IndexBotError` so without an exit code or
    a step summary. Structural faults those gates *do* cover — malformed CAS
    bytes, an index with no `manifests` key, a live-tag digest missing from
    `content_by_digest` — still propagate as `JSONDecodeError`/`KeyError`,
    matching `_cas_url`'s trust posture.
    """
    platforms: set[str] = set()
    for digest in _live_tag_content_digests(source.root):
        index = cast("dict[str, object]", json.loads(source.content_by_digest[f"{digest}.json"]))
        for descriptor in cast("list[dict[str, object]]", index["manifests"]):
            platform = descriptor.get("platform")
            if not isinstance(platform, dict):
                continue
            # Honest value type: JSON keys are strings, values are anything.
            fields = cast("dict[str, object]", platform)
            os_name, arch = fields.get("os"), fields.get("architecture")
            if not isinstance(os_name, str) or not isinstance(arch, str) or os_name == "unknown":
                continue
            platforms.add(f"{os_name}/{arch}")
    return sorted(platforms)


def _latest_activity(root: PackageRoot) -> str | None:
    """Lexicographic max over this root's tag `observed` values plus every
    yanked tag's `yanked.at` — the per-package "last activity" timestamp
    (catalog `updated` field), and the per-package term `_generated_timestamp`
    folds over. Same Z-anchored fixed-width timestamp invariant as
    `_generated_timestamp` (enforced upstream, never re-checked here).
    `None` when the root has never carried a tag."""
    timestamps: list[str] = []
    for entry in root.tags.values():
        timestamps.append(entry.observed)
        if entry.yanked is not None:
            timestamps.append(entry.yanked.at)
    return max(timestamps, default=None)


def _catalog_entry(source: SourcePackage) -> dict[str, object]:
    """One `/data/catalog/catalog.json` `packages[]` row — summary only, CAS
    URL refs for logo/readme rather than duplicated blob bytes (ADR-3's
    explicit divergence from `ocx-sh/ocx`'s website). Yanked tags are
    excluded from `tagCount`/`platforms` (plan Phase 2 WP-list).

    `variants` is **derived here** from `root.tags` via the one implementation
    of the rule (`core/version_order.py`'s `variant_names`) — the same call
    `core/validate_entry.py`'s `check_variants_match_tags` makes. The root's
    stored `variants` field is a projection of the same `tags` this row
    already holds, so reading it would only add a second source of truth; it
    is vestigial and scheduled for removal (ocx stops writing it, then the
    stored keys are swept). Until then it is still parsed and still preserved
    verbatim on rewrite — that byte preservation is what keeps an announce
    from rewriting every root that carries it.

    Derived over **all** tags, yanked included, matching `variant_names(root.
    tags)` upstream: a yank-filtered set here would be a different value from
    the one the PR gate enforces. Note `latestVersion` deliberately skips
    variant-prefixed tags (`find_latest_version`), so without this key the
    grid could not tell a package that ships variants from one that does not.
    Omitted when empty, matching the wire spelling, so every catalog row for
    a package with no variants is byte-identical to before the field existed.
    """
    namespace, package = source.package_id.namespace, source.package_id.package
    root = source.root
    desc = root.desc
    live_tag_count = sum(1 for entry in root.tags.values() if entry.yanked is None)
    ext_lookup = _cas_ext_lookup(source.content_by_digest)
    logo_digest = desc.logo if desc is not None else None
    readme_digest = desc.readme if desc is not None else None
    derived_variants = variant_names(root.tags)
    variants = {"variants": list(derived_variants)} if derived_variants else {}

    return {
        "namespace": namespace,
        "package": package,
        "name": root.name,
        "status": root.status,
        "deprecatedMessage": root.deprecated_message,
        "supersededBy": root.superseded_by,
        # Catalog sort keys (site "newest"/"recently updated" sorts):
        # announced date from the root, last activity from the tags.
        "created": root.created,
        "updated": _latest_activity(root),
        "title": desc.title if desc is not None else root.name,
        "description": desc.description if desc is not None else "",
        "keywords": list(desc.keywords) if desc is not None else [],
        "latestVersion": find_latest_version(root.tags),
        **variants,
        "tagCount": live_tag_count,
        "platforms": _catalog_platforms(source),
        "logoUrl": _cas_url(namespace, package, logo_digest, ext_lookup),
        "readmeUrl": _cas_url(namespace, package, readme_digest, ext_lookup),
    }


def _generated_timestamp(ordered: Sequence[SourcePackage]) -> str | None:
    """`/data/catalog/catalog.json`'s `generated` field — the lexicographic
    string max over every tag's `observed` value plus every yanked tag's
    `yanked.at` value, across every package in `ordered`. Fixed-shape UTC
    timestamps (`schema/root.schema.json`) make lexicographic order equal
    chronological order, so no `datetime` parsing is needed anywhere in this
    pure module — `render --check` stays idempotent regardless of wall
    clock. `None` when no package has ever carried a tag.

    **Invariant this function relies on and never itself checks**: every
    timestamp string it touches is Z-anchored and fixed-width
    (`YYYY-MM-DDThh:mm:ssZ`) — a legal-but-offset `+02:00` value would sort
    wrong here and silently produce the wrong package's timestamp as
    `generated`. `_generated_timestamp` trusts its input; the invariant is
    enforced upstream, at the input boundary, by
    `core/validate_entry.py`'s `check_tag_timestamps_z_anchored` (run by
    `cli/validate.py`'s PR gate) plus `schema/root.schema.json`'s
    `tagEntry.observed`/`yanked.at` pattern — never re-validated here.
    """
    timestamps = [
        activity for source in ordered if (activity := _latest_activity(source.root)) is not None
    ]
    return max(timestamps, default=None)


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


def _catalog_index(ordered: Sequence[SourcePackage]) -> str:
    """`data/catalog/catalog.json` — the catalog-grid view-model (frozen
    shape, plan_site_redesign): `generated` + `packages[]`, packages in the
    same package-id-sorted order as `ordered`."""
    catalog = {
        "generated": _generated_timestamp(ordered),
        "packages": [_catalog_entry(source) for source in ordered],
    }
    return json.dumps(catalog, indent=2) + "\n"


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

    dist_files.append(FileWrite(path="data/catalog/catalog.json", content=_catalog_index(ordered)))

    return tuple(dist_files)
