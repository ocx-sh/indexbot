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

A committed tag `observe_one_tag` now *refuses* — repointed at a bare image
manifest, or grown past the index size ceiling — escalates as
`tag-unrecordable`. The sweep catches that refusal per tag rather than letting
it propagate: one publisher repointing one tag must not abort the nightly run
for every other package, and the tag's `"digest-mismatch"` counterpart from
`core/verify_claims.py` is filtered out below as floating-tag drift, so
without this line the fault would leave no trace at all.

`"tag-missing-upstream"` (ADR-6 FP-2/FP-3 — a decided rule, not an open
question) **does** escalate, unless the committed `TagEntry.yanked is not
None` for that tag: yank is grace, an explicit owner-authorized exemption
from the registry-existence check; a tag vanishing from the registry with
no yank marker at all is an anomaly, not a silent drop (`_PackageReport`
carries the committed root's yanked-tag names so `_escalating_findings` can
tell the two apart).

`tag-unrecordable` takes that same grace, and for the same reason: both ask
what the registry currently serves for a tag the index may already have
disclaimed. A yanked tag repointed at a bare manifest and a yanked tag
deleted outright are one owner intent, and it would be incoherent to open a
nightly anomaly issue for the first while exempting the second. This is the
line the `cas-object-*` family sits on the other side of — those concern
bytes this index *stores* and is answerable for whatever the tag's
disposition, so no yank excuses them.

A non-empty escalating-finding set opens/updates one anomaly issue
(`ForgePort.create_or_update_issue`, promoted onto the port this stage) and
then raises `AnomalyError` (exit 65) once the full sweep completes — unless
`--anomaly-ok` is given.

## `--anomaly-ok`: folding the CI-side exit-code translation in

Both forges used to wrap this command in 3-4 shell steps that translated its
raw exit code into what the *job* should do: 0 -> nothing; 65 -> print a
notice pointing at the tracking issue this command already filed, and stay
green (the filed issue is the human signal; failing the nightly build on top
of an already-actioned condition is just alarm fatigue); 75 -> print an
error and fail (backoff exhausted, the next scheduled run is the retry);
anything else -> print an error and fail. `--anomaly-ok` is that middle
translation, folded into the command itself: a caller that passes it gets
`ExitCode.OK` instead of `AnomalyError` on a found-and-filed anomaly, plus
the same notice written via `_common.write_ci_summary` instead of a
hand-rolled `::warning`. It changes nothing else — a genuine `TransientError`
(75) still propagates unchanged either way, because a transient failure is
never "ok", and a caller that wants the raw ANOMALY (65) exit to detect the
condition itself simply omits the flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from ocx_indexbot.core.anomaly import check_tag_mutations
from ocx_indexbot.core.observe import observe_one_tag
from ocx_indexbot.core.policy import IndexPolicy
from ocx_indexbot.core.validate_entry import (
    check_repository_allowlisted,
    check_repository_shape,
    parse_package_id,
    parse_package_root,
)
from ocx_indexbot.core.verify_claims import verify_claims
from ocx_indexbot.errors import AnomalyError, ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import PackageId

from ._common import write_ci_summary

if TYPE_CHECKING:
    import argparse

    from ocx_indexbot.core.anomaly import AnomalyFinding
    from ocx_indexbot.core.observe import Observation
    from ocx_indexbot.core.verify_claims import ClaimFinding
    from ocx_indexbot.ports import FilePort, ForgePort, RegistryPort

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
    parser.add_argument(
        "--anomaly-ok",
        action="store_true",
        help=(
            "exit 0 (not 65) when an anomaly is found — it is already filed/updated as a "
            "tracking issue by this run, so a scheduled job that wants to stay green on an "
            "already-actioned condition and fail only on a genuine transient error (exit 75) "
            "sets this. Without it, reconcile keeps the raw ANOMALY exit-65 contract (ADR-4 "
            "BD-2) for a caller that wants to detect the anomaly itself."
        ),
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

    unrecordable_tags: tuple[str, ...]
    """`"<tag>: <reason>"` for every *live* committed tag whose current
    registry state `observe_one_tag` refuses outright — repointed at a bare
    image manifest, or grown past the size ceiling. Escalates: it is a
    structural fault, not the floating-tag digest drift `_escalates`
    deliberately tolerates. Yanked tags are filtered out at collection in
    `_verify_one` under the same ADR-6 FP-2/FP-3 grace `tag-missing-upstream`
    gets — the index has already disclaimed them, so what the registry now
    serves for one is not the index's anomaly."""


def _root_path(package_id: PackageId) -> str:
    return f"{_ROOT_PREFIX}{package_id}.json"


def _discover_package_ids(
    files: FilePort, *, scope: PackageId | None, name_segments: int
) -> tuple[PackageId, ...]:
    """Every `p/<namespace>/<package>.json` root, excluding CAS subtrees.

    A root is exactly `name_segments` path segments under `p/` whose last
    segment ends in `.json`; a CAS object lives deeper
    (`p/<ns>/<pkg>/o/sha256/<hex>.<ext>`) and is filtered out by the
    segment-count check alone.
    """
    ids: list[PackageId] = []
    for path in files.list_files(_ROOT_PREFIX):
        segments = path.removeprefix(_ROOT_PREFIX).split("/")
        # A root document sits at exactly the declared depth. Anything deeper
        # is that package's own CAS subtree (`.../o/sha256/<hex>.json`), which
        # this listing must not mistake for a root of its own.
        if len(segments) != name_segments or not segments[-1].endswith(".json"):
            continue
        segments[-1] = segments[-1].removesuffix(".json")
        ids.append(PackageId(segments=tuple(segments)))
    if scope is not None:
        ids = [package_id for package_id in ids if package_id == scope]
    return tuple(sorted(ids, key=str))


def _resolve_scope(args: argparse.Namespace, *, name_segments: int) -> PackageId | None:
    raw = getattr(args, "package", None)
    if not raw:
        return None
    return parse_package_id(raw, name_segments=name_segments)


def _cas_bytes_by_digest(
    files: FilePort, package_id: PackageId, wanted_digests: frozenset[str]
) -> dict[str, bytes]:
    """`wanted_digests` resolved to their already-committed bytes. A digest
    named by `root` with no matching CAS file at all is simply absent from
    the returned map — `verify_claims` reports that as
    `cas-object-missing`/`desc-blob-missing`, never a `KeyError` here."""
    prefix = f"{_ROOT_PREFIX}{package_id}/o/sha256/"
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
    package_id: PackageId,
    *,
    files: FilePort,
    registry: RegistryPort,
    policy: IndexPolicy,
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
    check_repository_allowlisted(root.repository, policy.registry_hosts)
    check_repository_shape(root.repository)

    yanked_tags = frozenset(tag for tag, entry in root.tags.items() if entry.yanked is not None)

    observations: list[Observation] = []
    unrecordable: list[str] = []
    for tag in root.tags:
        try:
            observation = observe_one_tag(root.repository, tag, registry)
        except ValidationError as error:
            if tag not in yanked_tags:
                unrecordable.append(f"{tag}: {error}")
            continue
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
    return _PackageReport(
        package_id=package_id,
        pinned_mutations=pinned_mutations,
        claim_findings=claim_findings,
        yanked_tags=yanked_tags,
        unrecordable_tags=tuple(unrecordable),
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
        f"{report.package_id} tag-unrecordable: {entry}" for entry in report.unrecordable_tags
    )
    lines.extend(
        f"{report.package_id} {finding.kind}: {finding.detail}"
        for finding in report.claim_findings
        if _escalates(finding, yanked_tags=report.yanked_tags)
    )
    return tuple(lines)


def run(
    args: argparse.Namespace,
    *,
    files: FilePort,
    registry: RegistryPort,
    github: ForgePort,
    policy: IndexPolicy,
) -> ExitCode:
    """Full-index verify-only sweep. `args.package` (optional
    `<namespace>/<package>` scope string) is read if present, defaulting to
    "verify everything" when absent.

    Ports are explicit keyword arguments rather than constructed inside this
    module (functional core / imperative shell) — `cli/_wiring.py` supplies
    the real adapters; tests supply `tests/fakes`.
    """
    scope = _resolve_scope(args, name_segments=policy.name_segments)
    package_ids = _discover_package_ids(files, scope=scope, name_segments=policy.name_segments)

    findings: list[str] = []
    checked = 0
    for package_id in package_ids:
        report = _verify_one(package_id, files=files, registry=registry, policy=policy)
        if report is None:
            continue
        checked += 1
        findings.extend(_escalating_findings(report))

    if findings:
        detail = "; ".join(findings)
        summary = f"verified {checked} package(s); {len(findings)} anomaly(ies): {detail}"
        github.create_or_update_issue(title=_ISSUE_TITLE, body=summary, labels=["anomaly"])
        print(summary)
        if cast(bool, getattr(args, "anomaly_ok", False)):
            # The CI-side translation `.github/workflows/reconcile.yml` and the
            # GitLab lane used to do in shell: the tracking issue above is
            # already the human signal (ADR-6 FP-3, never auto-healed), so
            # this stays green rather than failing the run on top of an
            # already-actioned condition. Findings go inside a fenced block —
            # never an annotation title — because they can carry
            # registry-observed tag names and digests (BD-4).
            write_ci_summary(
                "indexbot reconcile: anomaly detected",
                "An integrity anomaly was detected; the tracking issue is already "
                f"filed/updated (never auto-healed, ADR-6 FP-3). Findings:\n\n```\n{detail}\n```",
            )
            return ExitCode.OK
        raise AnomalyError(summary)

    summary = f"verified {checked} package(s); 0 anomalies"
    print(summary)
    return ExitCode.OK
