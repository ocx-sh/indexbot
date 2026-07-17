"""Package-root regeneration from freshly observed tags (ADR-1 D2;
CONTRACTS.md §7).

`current` is always the already-committed root — a namespace with no root
yet is a validation error the caller raises *before* calling `regenerate`
(namespace claiming, ADR-2 ND-5, is a separate human-PR flow that commits a
root with empty `tags` before the first `announce` ever runs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indexbot.model import PackageRoot, TagEntry

if TYPE_CHECKING:
    from indexbot.core.observe import Observation
    from indexbot.model import Desc
    from indexbot.ports import ClockPort


def regenerate(
    current: PackageRoot,
    observations: tuple[Observation, ...],
    desc: Desc | None,
    clock: ClockPort,
) -> PackageRoot:
    """Rebuild `current.tags` from `observations`; every other field is
    carried over verbatim from `current` (human-governed, G-09).

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
        tags=new_tags,
    )
