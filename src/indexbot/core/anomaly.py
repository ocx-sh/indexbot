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

from indexbot.core.version_order import is_full_release_version
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
    `core/version_order.is_full_release_version` classifies `True` (pinned —
    an exact, unprefixed `X.Y.Z`), a different content digest between
    `committed` and `fresh` is one `AnomalyFinding`. Tags classified `False`
    (`latest`, partial versions, variant-prefixed) are floating and are
    never flagged regardless of digest change — the expected cascade-push
    behavior (ADR-1 D2/D3).

    **Open question, flagged loudly rather than silently resolved**
    (CONTRACTS.md §7/§13 item 3): this pinned-vs-floating predicate is the
    Contracts stage's best-effort reading of ADR-1/ADR-4, not a confirmed
    decision. A wrong default here either misses real tamper (too
    permissive) or fires false-positive anomalies on legitimate cascade
    pushes (too strict). Confirm with the owner before Phase 3's E2E gate.
    """
    fresh_by_tag = {observation.tag: observation.content_digest for observation in fresh}
    findings: list[AnomalyFinding] = []
    for tag, entry in committed.tags.items():
        if not is_full_release_version(tag):
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
