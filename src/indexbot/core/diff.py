"""Root-to-root patch computation and PR classification (ADR-1 D2/D3;
CONTRACTS.md §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from indexbot.model import ObservationObject, PackageId, PackageRoot

if TYPE_CHECKING:
    from indexbot.core.observe import Observation

ChangeClass = Literal["new-package", "refresh", "human-review-required"]


@dataclass(frozen=True, slots=True)
class Patch:
    package_id: PackageId
    root: PackageRoot  # target — write verbatim (validate_entry.serialize_package_root)
    new_objects: tuple[tuple[str, ObservationObject], ...]
    summary: str  # one-line PR-body fragment, e.g. "+3.29.0, ~latest -> sha256:bbbb"


def _tag_diff_summary(current: PackageRoot, target: PackageRoot) -> str:
    parts: list[str] = []
    for tag in sorted(target.tags):
        if tag not in current.tags:
            parts.append(f"+{tag}")
        elif current.tags[tag].content != target.tags[tag].content:
            short = target.tags[tag].content.removeprefix("sha256:")[:12]
            parts.append(f"~{tag} -> sha256:{short}")
    for tag in sorted(current.tags):
        if tag not in target.tags:
            parts.append(f"-{tag}")
    return ", ".join(parts) if parts else "metadata updated"


def diff(
    package_id: PackageId,
    current: PackageRoot,
    target: PackageRoot,
    observations: tuple[Observation, ...],
) -> Patch | None:
    """`None` iff `current == target` structurally (dataclass equality —
    both are frozen, so this is a plain `==`). Otherwise a `Patch`.

    **Deviation from CONTRACTS.md §7's literal 2-argument
    `diff(current, target)` signature — flagged here, not silently applied.**
    `Patch.package_id: PackageId` and
    `Patch.new_objects: tuple[tuple[str, ObservationObject], ...]` cannot be
    produced from `current`/`target` alone: `PackageRoot` carries neither a
    `PackageId` (only the string `name`) nor any `ObservationObject`
    payload (only `TagEntry.content` digest strings). `package_id` and
    `observations` — the same tuple `core/observe.py`'s `observe()` already
    produced earlier in the `cli/announce.py`/`cli/reconcile.py` pipeline —
    are the two additional inputs needed to resolve digests to their
    objects. See `open_questions`.

    `new_objects` is every digest referenced by `target.tags` that does not
    already appear in `current.tags` (already-existing objects — shared
    digest / cascade aliasing, ADR-1 D3 — are excluded so `cli/announce.py`
    never re-writes a CAS object that's already committed), resolved
    against `observations` by content digest. A `target` digest absent from
    both `current.tags` and `observations` is a caller bug (`regenerate`'s
    only source of new digests is `observations`) and raises `KeyError`
    rather than silently dropping the object.
    """
    if current == target:
        return None
    existing_digests = {entry.content for entry in current.tags.values()}
    by_digest = {observation.content_digest: observation.object for observation in observations}
    new_digests = sorted({entry.content for entry in target.tags.values()} - existing_digests)
    new_objects = tuple((digest, by_digest[digest]) for digest in new_digests)
    return Patch(
        package_id=package_id,
        root=target,
        new_objects=new_objects,
        summary=_tag_diff_summary(current, target),
    )


def classify_change(before: PackageRoot | None, after: PackageRoot) -> ChangeClass:
    """`cli/classify_pr.py`'s core. `before` is the base-ref root, `None` if
    the PR added a brand-new `p/<ns>/<pkg>.json` (the path did not exist at
    the base ref — G-04). `before is None` -> always `"new-package"`.

    Otherwise the machine lane is narrow by design: `"refresh"` only when
    the sole change is to `tags` (membership or content) plus the yanked-marker
    rule below. **Any other field changing is `"human-review-required"`** —
    every governance field `product-context.md` lists is human-authored, so
    an owner-authored refresh PR must never silently carry a governance edit
    through auto-merge. Concretely: `repository`, `owners`, `status`,
    `deprecated_message`, `created`, `upstream`, or `superseded_by` differing
    -> `"human-review-required"`, OR any tag present in both `before.tags`
    and `after.tags` has a different `yanked` value (G-05's expanded key set,
    ADR-4 disposition table) — else `"refresh"`. `name` is deliberately not
    checked here — it's pinned by `check_name_matches_path` instead, a
    structural invariant, not a governance-vs-machine distinction. `desc` is
    deliberately not checked here either — it's bot-derived from the
    registry's `__ocx.desc` tag (`core/desc.py`), not human-authored, so it
    stays in the machine lane alongside `tags`.
    """
    if before is None:
        return "new-package"
    governance_changed = (
        before.repository != after.repository
        or before.owners != after.owners
        or before.status != after.status
        or before.deprecated_message != after.deprecated_message
        or before.created != after.created
        or before.upstream != after.upstream
        or before.superseded_by != after.superseded_by
    )
    if governance_changed:
        return "human-review-required"
    for tag, before_entry in before.tags.items():
        after_entry = after.tags.get(tag)
        if after_entry is not None and after_entry.yanked != before_entry.yanked:
            return "human-review-required"
    return "refresh"
