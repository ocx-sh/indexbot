"""Claim verification — fork-PR announce revamp (owner-confirmed decision
set, 2026-07-18: "Verify-only reconcile" + "byte-exact discipline").

Re-derives every *claimed* tag/desc-blob digest in a committed `PackageRoot`
individually from registry truth. Deliberately **subset semantics**: this
never asserts full-set equality against `registry.list_tags()` — the owner's
curated `tags` map may legitimately be a subset of what the physical
registry carries (that is the entire point of owner curation, ADR decision
set item 2 — announce is the only add/remove authority, and an owner may
simply choose not to announce every registry tag).

Pure given its injected `RegistryPort` and already-read CAS bytes
(CONTRACTS.md §0) — every finding is *returned*, never raised. The caller
decides disposition: `cli/validate.py`'s unprivileged PR gate treats any
finding as a `ValidationError` (reject the PR — nothing was ever
legitimately observed to mutate, the claim just isn't true right now);
`cli/reconcile.py`'s nightly sweep escalates most finding kinds to an
anomaly against already-committed history (exit 65), except a
`"digest-mismatch"` on a floating (non-pinned) tag, and a
`"tag-missing-upstream"` on a *yanked* tag (ADR-6 FP-2/FP-3 — yank is grace,
an explicit exemption from the registry-existence check; see
`cli/reconcile.py`'s module docstring for the full disposition table) — same
taxonomy, two different dispositions for two different trust contexts,
mirroring `core/anomaly.py`'s existing finding-not-exception design.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from indexbot.core.observe import observe_one_tag
from indexbot.model import PackageId, PackageRoot

if TYPE_CHECKING:
    from collections.abc import Mapping

    from indexbot.ports import RegistryPort

FindingKind = Literal[
    "tag-missing-upstream",
    "digest-mismatch",
    "cas-object-missing",
    "cas-object-hash-mismatch",
    "desc-blob-missing",
    "desc-blob-hash-mismatch",
]


@dataclass(frozen=True, slots=True)
class ClaimFinding:
    """One claim that failed re-derivation from registry truth.

    `detail` is the claimed tag name for a `tags[*]` claim, or the literal
    `"desc.readme"`/`"desc.logo"` for a desc-blob claim. An empty tuple from
    `verify_claims` means every claim checked out clean.
    """

    package_id: PackageId
    kind: FindingKind
    detail: str


def _digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _verify_blob_claim(
    package_id: PackageId,
    label: str,
    digest: str,
    cas_object_bytes: Mapping[str, bytes],
    *,
    missing_kind: FindingKind,
    mismatch_kind: FindingKind,
) -> ClaimFinding | None:
    """Shared CAS-hash-check for one already-known-correct claimed digest (a
    tag's re-derived `content_digest`, or a desc readme/logo digest) —
    every such claim needs exactly "bytes present, bytes hash to the claimed
    digest", just a different finding kind/label per caller."""
    blob = cas_object_bytes.get(digest)
    if blob is None:
        return ClaimFinding(package_id=package_id, kind=missing_kind, detail=label)
    if _digest_of(blob) != digest:
        return ClaimFinding(package_id=package_id, kind=mismatch_kind, detail=label)
    return None


def _verify_tag_claim(
    package_id: PackageId,
    tag: str,
    content_digest: str,
    repository: str,
    registry: RegistryPort,
    cas_object_bytes: Mapping[str, bytes],
) -> ClaimFinding | None:
    observation = observe_one_tag(repository, tag, registry)
    if observation is None:
        return ClaimFinding(package_id=package_id, kind="tag-missing-upstream", detail=tag)
    if observation.content_digest != content_digest:
        return ClaimFinding(package_id=package_id, kind="digest-mismatch", detail=tag)
    return _verify_blob_claim(
        package_id,
        tag,
        content_digest,
        cas_object_bytes,
        missing_kind="cas-object-missing",
        mismatch_kind="cas-object-hash-mismatch",
    )


def verify_claims(
    package_id: PackageId,
    root: PackageRoot,
    cas_object_bytes: Mapping[str, bytes],
    registry: RegistryPort,
) -> tuple[ClaimFinding, ...]:
    """Verify every claim in `root` individually against `registry` (subset
    semantics — see module docstring).

    Each `root.tags[*]` entry re-derives via `core/observe.py`'s
    `observe_one_tag` and must (a) still resolve on the registry, (b)
    re-derive to the exact same `content_digest`, and (c) have matching CAS
    bytes already present in `cas_object_bytes` (keyed by digest string) that
    hash to that same digest. `root.desc.readme`/`.logo`, when set, get the
    same CAS-hash check ((b) does not apply to them — a desc blob's digest
    is compared against the registry's floating `__ocx.desc` tag,
    `core/desc.py`'s concern, not re-derived here); this closes the gap
    where only tag digests were ever byte-verified.
    """
    findings: list[ClaimFinding] = []
    for tag, entry in sorted(root.tags.items()):
        finding = _verify_tag_claim(
            package_id, tag, entry.content, root.repository, registry, cas_object_bytes
        )
        if finding is not None:
            findings.append(finding)
    if root.desc is not None:
        if root.desc.readme is not None:
            finding = _verify_blob_claim(
                package_id,
                "desc.readme",
                root.desc.readme,
                cas_object_bytes,
                missing_kind="desc-blob-missing",
                mismatch_kind="desc-blob-hash-mismatch",
            )
            if finding is not None:
                findings.append(finding)
        if root.desc.logo is not None:
            finding = _verify_blob_claim(
                package_id,
                "desc.logo",
                root.desc.logo,
                cas_object_bytes,
                missing_kind="desc-blob-missing",
                mismatch_kind="desc-blob-hash-mismatch",
            )
            if finding is not None:
                findings.append(finding)
    return tuple(findings)
