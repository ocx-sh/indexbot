"""`indexbot reconcile` — verify-only nightly sweep (fork-PR announce revamp,
owner-confirmed decision set 2026-07-18: "Verify-only reconcile").

Never writes to `p/` — no regenerate, no diff, no commit, no PR. For every
committed `p/<namespace>/<package>.json`: re-derive each *claimed* tag/desc
blob from registry truth (`core/verify_claims.py`) plus `core/anomaly.py`'s
existing pinned-tag mutation check (reused verbatim, not reinvented — a
still-resolving pinned tag whose content changed is exactly what
`check_tag_mutations` already detects, given each committed tag's freshly
re-observed state).

Disposition (which findings actually escalate to the exit-65 anomaly
outcome): `core/anomaly.py`'s pinned-tag mutations always escalate.
`core/verify_claims.py`'s `"cas-object-missing"`/`"cas-object-hash-mismatch"`/
`"desc-blob-missing"`/`"desc-blob-hash-mismatch"` findings always escalate too
— structural CAS-integrity concerns, independent of tag semantics (the exact
same unconditional treatment `core/validate_entry.py`'s
`check_no_dangling_references`/`check_digest_self_consistent` already give
them). `verify_claims`'s `"digest-mismatch"` does **not** escalate on its
own: a floating tag (`latest`, partial versions, variant-prefixed) drifting
is the expected cascade-push behavior (ADR-1 D2/D3) — that exact same
digest-mismatch, on a *pinned* tag, is already caught by the reused
`check_tag_mutations` check above, so this avoids double-flagging one
underlying phenomenon through two different finding shapes.

`"tag-missing-upstream"` (ADR-6 FP-2/FP-3 — a decided rule, not an open
question) **does** escalate, unless the committed `TagEntry.yanked is not
None` for that tag: yank is grace, an explicit owner-authorized exemption
from the registry-existence check; a tag vanishing from the registry with
no yank marker at all is an anomaly, not a silent drop (`_PackageReport`
carries the committed root's yanked-tag names so `_escalating_findings` can
tell the two apart).

A non-empty escalating-finding set opens/updates one anomaly issue
(`GitHubPort.create_or_update_issue`, promoted onto the port this stage) and
then raises `AnomalyError` (exit 65) once the full sweep completes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from indexbot.core.anomaly import check_tag_mutations
from indexbot.core.observe import observe_one_tag
from indexbot.core.validate_entry import (
    check_repository_allowlisted,
    check_repository_shape,
    parse_package_id,
    parse_package_root,
)
from indexbot.core.verify_claims import verify_claims
from indexbot.errors import AnomalyError
from indexbot.exit_codes import ExitCode
from indexbot.model import PackageId

if TYPE_CHECKING:
    import argparse

    from indexbot.core.anomaly import AnomalyFinding
    from indexbot.core.observe import Observation
    from indexbot.core.verify_claims import ClaimFinding
    from indexbot.ports import FilePort, GitHubPort, RegistryPort

_ROOT_PREFIX: Final[str] = "p/"
_ISSUE_TITLE: Final[str] = "indexbot reconcile: anomalies detected"
_ESCALATING_CLAIM_KINDS: Final[frozenset[str]] = frozenset(
    {
        "cas-object-missing",
        "cas-object-hash-mismatch",
        "desc-blob-missing",
        "desc-blob-hash-mismatch",
    }
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `reconcile`'s CLI surface. `--dry-run` is gone
    — verify-only reconcile never writes at all, so there is nothing left
    for a dry run to skip."""
    parser.add_argument(
        "--package", default=None, help="scope the sweep to one <namespace>/<package>"
    )


@dataclass(frozen=True, slots=True)
class _PackageReport:
    package_id: PackageId
    pinned_mutations: tuple[AnomalyFinding, ...]
    claim_findings: tuple[ClaimFinding, ...]
    yanked_tags: frozenset[str]
    """Committed tag names with a non-`None` `TagEntry.yanked` marker — the
    grace exemption `_escalating_findings` checks a `"tag-missing-upstream"`
    finding's tag name against (ADR-6 FP-2/FP-3)."""


def _root_path(package_id: PackageId) -> str:
    return f"{_ROOT_PREFIX}{package_id.namespace}/{package_id.package}.json"


def _discover_package_ids(files: FilePort, *, scope: PackageId | None) -> tuple[PackageId, ...]:
    """Every `p/<namespace>/<package>.json` root, excluding CAS subtrees.

    A root is exactly two path segments under `p/` whose second segment ends
    in `.json`; a CAS object lives three-plus segments deep
    (`p/<ns>/<pkg>/o/sha256/<hex>.<ext>`) and is filtered out by the
    segment-count check alone.
    """
    ids: list[PackageId] = []
    for path in files.list_files(_ROOT_PREFIX):
        segments = path.removeprefix(_ROOT_PREFIX).split("/")
        if len(segments) != 2 or not segments[1].endswith(".json"):
            continue
        namespace, filename = segments
        ids.append(PackageId(namespace=namespace, package=filename.removesuffix(".json")))
    if scope is not None:
        ids = [package_id for package_id in ids if package_id == scope]
    return tuple(sorted(ids, key=str))


def _resolve_scope(args: argparse.Namespace) -> PackageId | None:
    raw = getattr(args, "package", None)
    if not raw:
        return None
    return parse_package_id(raw)


def _cas_bytes_by_digest(
    files: FilePort, package_id: PackageId, wanted_digests: frozenset[str]
) -> dict[str, bytes]:
    """`wanted_digests` resolved to their already-committed bytes. A digest
    named by `root` with no matching CAS file at all is simply absent from
    the returned map — `verify_claims` reports that as
    `cas-object-missing`/`desc-blob-missing`, never a `KeyError` here."""
    prefix = f"{_ROOT_PREFIX}{package_id.namespace}/{package_id.package}/o/sha256/"
    paths_by_digest = {
        f"sha256:{path.rsplit('/', 1)[-1].split('.', 1)[0]}": path
        for path in files.list_files(prefix)
    }
    return {
        digest: cast(bytes, files.read_bytes(path))
        for digest, path in paths_by_digest.items()
        if digest in wanted_digests
    }


def _verify_one(
    package_id: PackageId, *, files: FilePort, registry: RegistryPort
) -> _PackageReport | None:
    """One package's verify-only sweep step. `None` if the root vanished
    between `list_files` and this read (the same race the previous
    regenerate-based design tolerated for an individual root — not fatal)."""
    raw = files.read_bytes(_root_path(package_id))
    if raw is None:
        return None
    root = parse_package_root(raw)

    # SSRF ordering (G-03, ADR-4 BD-1): must run before any RegistryPort
    # call below.
    check_repository_allowlisted(root.repository)
    check_repository_shape(root.repository)

    observations: list[Observation] = []
    for tag in root.tags:
        observation = observe_one_tag(root.repository, tag, registry)
        if observation is not None:
            observations.append(observation)
    pinned_mutations = check_tag_mutations(package_id, root, tuple(observations))

    desc_digests: frozenset[str] = (
        frozenset(digest for digest in (root.desc.readme, root.desc.logo) if digest is not None)
        if root.desc is not None
        else frozenset()
    )
    wanted_digests: frozenset[str] = (
        frozenset(entry.content for entry in root.tags.values()) | desc_digests
    )
    cas_bytes = _cas_bytes_by_digest(files, package_id, wanted_digests)
    claim_findings = verify_claims(package_id, root, cas_bytes, registry)
    yanked_tags = frozenset(tag for tag, entry in root.tags.items() if entry.yanked is not None)
    return _PackageReport(
        package_id=package_id,
        pinned_mutations=pinned_mutations,
        claim_findings=claim_findings,
        yanked_tags=yanked_tags,
    )


def _escalates(finding: ClaimFinding, *, yanked_tags: frozenset[str]) -> bool:
    """ADR-6 FP-2/FP-3: `"tag-missing-upstream"` escalates unless the
    claimed tag is yanked (yank = grace, an explicit exemption from the
    registry-existence check) — every other escalating kind is
    unconditional."""
    if finding.kind == "tag-missing-upstream":
        return finding.detail not in yanked_tags
    return finding.kind in _ESCALATING_CLAIM_KINDS


def _escalating_findings(report: _PackageReport) -> tuple[str, ...]:
    lines = [
        f"{report.package_id} {finding.tag}: pinned-tag-mutation "
        f"committed={finding.committed_content} fresh={finding.fresh_content}"
        for finding in report.pinned_mutations
    ]
    lines.extend(
        f"{report.package_id} {finding.kind}: {finding.detail}"
        for finding in report.claim_findings
        if _escalates(finding, yanked_tags=report.yanked_tags)
    )
    return tuple(lines)


def run(
    args: argparse.Namespace, *, files: FilePort, registry: RegistryPort, github: GitHubPort
) -> ExitCode:
    """Full-index verify-only sweep. `args.package` (optional
    `<namespace>/<package>` scope string) is read if present, defaulting to
    "verify everything" when absent.

    Ports are explicit keyword arguments rather than constructed inside this
    module (functional core / imperative shell) — `cli/_wiring.py` supplies
    the real adapters; tests supply `tests/fakes`.
    """
    scope = _resolve_scope(args)
    package_ids = _discover_package_ids(files, scope=scope)

    findings: list[str] = []
    checked = 0
    for package_id in package_ids:
        report = _verify_one(package_id, files=files, registry=registry)
        if report is None:
            continue
        checked += 1
        findings.extend(_escalating_findings(report))

    if findings:
        detail = "; ".join(findings)
        summary = f"verified {checked} package(s); {len(findings)} anomaly(ies): {detail}"
        github.create_or_update_issue(title=_ISSUE_TITLE, body=summary, labels=["anomaly"])
        print(summary)
        raise AnomalyError(summary)

    summary = f"verified {checked} package(s); 0 anomalies"
    print(summary)
    return ExitCode.OK
