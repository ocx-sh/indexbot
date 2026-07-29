"""Package-root regeneration from freshly observed tags (ADR-1 D2;
CONTRACTS.md §7).

`current` is always the already-committed root — a namespace with no root
yet is a validation error the caller raises *before* calling `regenerate`
(namespace claiming, ADR-2 ND-5, is a separate human-PR flow that commits a
root with empty `tags` before the first `announce` ever runs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indexbot.core.version_order import find_latest_version
from indexbot.model import PackageRoot, TagEntry

if TYPE_CHECKING:
    from indexbot.core.observe import Observation
    from indexbot.model import Desc
    from indexbot.ports import ClockPort


def _source_of_latest_version(
    tags: dict[str, TagEntry], observations: tuple[Observation, ...]
) -> str | None:
    """`root.source` — the `org.opencontainers.image.source` annotation of
    the *latest version* tag's observation (`core/observe.py`), or `None`.

    One root-level value rather than one per tag: provenance barely varies
    across a package's tags, and `tagEntry` is the hottest wire object.
    `find_latest_version` (the same predicate `/data/catalog/catalog.json`'s
    `latestVersion` uses) picks the tag; a package whose observed set carries
    no plain version tag at all (only `latest`, or only variant-prefixed
    tags) therefore has no `source`, which is why the field is optional in
    `schema/root.schema.json` rather than merely nullable.

    Re-derived wholesale from `observations` on every run, like `tags` — a
    stale annotation never survives an announce that no longer sees it.
    """
    latest_tag = find_latest_version(tags)
    return next((obs.source for obs in observations if obs.tag == latest_tag), None)


def regenerate(
    current: PackageRoot,
    observations: tuple[Observation, ...],
    desc: Desc | None,
    clock: ClockPort,
) -> PackageRoot:
    """Rebuild `current.tags` from `observations`; every other field
    (including `superseded_by`) is carried over verbatim from `current`
    (human-governed, G-09).

    `desc`: pass `current.desc` unchanged when `core/desc.py` found no
    change, or the new `Desc` from a non-`None` `DescUpdate.desc` when it
    did — `regenerate` does not call `core/desc.py` itself, the caller
    composes both.

    `tags`: a tag whose `content_digest` equals `current.tags[tag].content`
    keeps that entry's `observed` timestamp **unchanged** — no gratuitous
    timestamp churn on a no-op re-observe, which is what makes "run twice,
    second diff empty" hold. A new or changed-content tag gets
    `observed = clock.now_iso8601()`. An existing `yanked` marker survives
    untouched (human-governed, G-05) even if that tag's content also
    changed this run. A tag present in `current.tags` but absent from
    `observations` (removed upstream) is dropped.

    `source`: re-derived from `observations`, never carried over from
    `current` — see `_source_of_latest_version`. It is one of two non-`tags`
    fields this function computes rather than copies, because (like `desc`,
    which the caller supplies) it is registry-derived, not human-governed.

    `variants`: **never recorded** — always empty, which the serializer spells
    as the key being absent. The field is retired. `core/render.py` derives the
    catalog's `variants` from `tags` (#110), and `check_variants_match_tags`
    accepts an absent one while still holding a present one to the derivation
    (#112), so nothing reads what this function would write.

    Removing rather than carrying over is the load-bearing half. This is not
    the only publisher — `ocx package announce` writes the same roots and has
    stopped recording the field. If this function carried a committed value
    through, the two writers would alternate: one restores the key, the next
    opens a pull request whose entire diff is deleting it again, with no tag
    having moved. Both writers must remove it for the byte-identity that C6's
    unchanged short-circuit depends on to hold across them.
    """
    new_tags: dict[str, TagEntry] = {}
    for observation in observations:
        existing = current.tags.get(observation.tag)
        if existing is not None and existing.content == observation.content_digest:
            new_tags[observation.tag] = existing
            continue
        new_tags[observation.tag] = TagEntry(
            content=observation.content_digest,
            observed=clock.now_iso8601(),
            yanked=existing.yanked if existing is not None else None,
        )
    return PackageRoot(
        name=current.name,
        repository=current.repository,
        owners=current.owners,
        status=current.status,
        deprecated_message=current.deprecated_message,
        created=current.created,
        desc=desc,
        upstream=current.upstream,
        superseded_by=current.superseded_by,
        source=_source_of_latest_version(new_tags, observations),
        variants=(),
        tags=new_tags,
    )
