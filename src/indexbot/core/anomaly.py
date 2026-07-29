"""Tamper detection on pinned tags (ADR-1 D2/D5 verifiability chain;
CONTRACTS.md §7).

Returns findings — never raises `AnomalyError` itself. `cli/reconcile.py`
maps a non-empty result to the anomaly exit code (partial-success
semantics: clean-subset PR + one anomaly issue listing every finding + exit
65).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from indexbot.core.version_order import is_build_pinned_version
from indexbot.model import PackageId, PackageRoot

if TYPE_CHECKING:
    from indexbot.core.observe import Observation


@dataclass(frozen=True, slots=True)
class AnomalyFinding:
    package_id: PackageId
    tag: str
    committed_content: str
    fresh_content: str


def check_tag_mutations(
    package_id: PackageId, committed: PackageRoot, fresh: tuple[Observation, ...]
) -> tuple[AnomalyFinding, ...]:
    """Empty tuple = clean.

    For every tag present in both `committed.tags` and `fresh` that
    `core/version_order.is_build_pinned_version` classifies `True` (pinned —
    a version carrying a build fragment, `3.28.1_20260216`), a different
    content digest between `committed` and `fresh` is one `AnomalyFinding`.
    Tags classified `False` — `latest`, `3`, `3.28`, `3.28.1`, a bare variant
    name, any opaque tag — are the rolling cascade targets and are never
    flagged regardless of digest change: moving them is what a publish *is*
    (ADR-1 D2/D3, `crates/ocx_lib/src/package/cascade.rs`).

    **Resolved** (CONTRACTS.md §7/§13 item 3, 2026-07-29). The predicate was
    the exact inverse of this until now — it checked `X.Y.Z` and skipped
    `X.Y.Z_<build>`, so on the live index every one of the 49 immutable tags
    was exempt and all 71 checked tags were ones the cascade is supposed to
    move. Both failure modes were live at once: a force-repointed build tag
    passed silently, and the next legitimate republish of any package would
    have filed a tamper issue against its own rolling tags.

    Rolling tags stay exempt outright rather than getting a weaker check
    (forward-only, or "must land on a digest some build tag also carries").
    Neither is decidable from what the sweep observes: it reads only the tags
    already committed in the root, so the newly published build tag a
    legitimate cascade points at is not in the observation set at all, and
    tag ordering is not carried either. Doing it properly means listing tags
    from the registry — a different sweep, not a tightening of this one.
    """
    fresh_by_tag = {observation.tag: observation.content_digest for observation in fresh}
    findings: list[AnomalyFinding] = []
    for tag, entry in committed.tags.items():
        if not is_build_pinned_version(tag):
            continue
        fresh_content = fresh_by_tag.get(tag)
        if fresh_content is not None and fresh_content != entry.content:
            findings.append(
                AnomalyFinding(
                    package_id=package_id,
                    tag=tag,
                    committed_content=entry.content,
                    fresh_content=fresh_content,
                )
            )
    return tuple(findings)
