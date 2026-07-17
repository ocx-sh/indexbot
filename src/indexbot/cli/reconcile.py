"""`indexbot reconcile` — full-index nightly sweep (ADR-4 BD-1/BD-2;
CONTRACTS.md §12).

For every committed `p/<namespace>/<package>.json`: re-observe the physical
registry, recompute `desc`, check pinned-tag anomalies, regenerate, and diff
against the committed root. Packages with a clean diff are batched into one
commit + one PR (or, under `--dry-run`, only reported — no `GitHubPort` write
call is made at all). Packages with an anomaly finding are excluded from that
PR and their findings are collected instead.

Partial-success semantics (CONTRACTS.md §12, plan Phase 2 WP-list): the clean
subset's PR (if any) is opened *before* this module raises. A non-empty
finding set always raises `AnomalyError` (exit 65) after the sweep completes,
regardless of whether the clean-subset PR also succeeded — both outcomes are
visible to the caller.

Issue-creation on anomaly (CONTRACTS.md §13 item 4) happens at the *workflow*
layer, not here: `reconcile.yml`'s "Open or update anomaly issue" step reads
this module's exit code (65) and stderr/log output via `gh issue`, matching
`adapters/github_api.py`'s `GitHubApi.create_or_update_issue` docstring, which
notes that capability is deliberately not on `ports.GitHubPort` yet. This
module never needs an issue-creation port method — see `open_questions`.

A `TransientError`/`ValidationError` raised mid-sweep (registry backoff
exhausted, or a committed root somehow fails to parse) propagates uncaught
and aborts the *entire* sweep immediately — no partial commit of
already-collected clean patches. This extends `core/observe.py`'s per-call
"no partial-tag silent-skip" framing (BD-2) to the full-index sweep; neither
ADR states the sweep-level granularity explicitly, so this is flagged in
`open_questions` rather than silently assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from indexbot.cli._common import write_github_output
from indexbot.core.anomaly import check_tag_mutations
from indexbot.core.desc import check_desc_change
from indexbot.core.diff import diff
from indexbot.core.observe import observe
from indexbot.core.regenerate import regenerate
from indexbot.core.validate_entry import (
    cas_relpath,
    check_repository_allowlisted,
    check_repository_shape,
    parse_package_root,
    serialize_observation_object,
    serialize_package_root,
)
from indexbot.core.validate_payload import parse_package_id
from indexbot.errors import AnomalyError
from indexbot.exit_codes import ExitCode
from indexbot.model import PackageId

if TYPE_CHECKING:
    import argparse

    from indexbot.core.anomaly import AnomalyFinding
    from indexbot.core.desc import DescUpdate
    from indexbot.core.diff import Patch
    from indexbot.ports import ClockPort, FilePort, GitHubPort, RegistryPort

_ROOT_PREFIX = "p/"
_BASE_BRANCH = "main"
_RECONCILE_BRANCH = "indexbot/reconcile"
_PNG_MAGIC = b"\x89PNG"


@dataclass(frozen=True, slots=True)
class _PackageOutcome:
    """One package's sweep result — `None` fields mean "nothing to do"."""

    findings: tuple[AnomalyFinding, ...]
    patch: Patch | None
    cas_writes: dict[str, bytes]


def _root_path(package_id: PackageId) -> str:
    return f"{_ROOT_PREFIX}{package_id.namespace}/{package_id.package}.json"


def _discover_package_ids(files: FilePort, *, scope: PackageId | None) -> tuple[PackageId, ...]:
    """Every `p/<namespace>/<package>.json` root, excluding CAS subtrees.

    A root is exactly two path segments under `p/` whose second segment ends
    in `.json` (CONTRACTS.md §12); a CAS object lives three-plus segments
    deep (`p/<ns>/<pkg>/o/sha256/<hex>.<ext>`) and is filtered out by the
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


def _desc_cas_writes(package_id: PackageId, update: DescUpdate) -> dict[str, bytes]:
    """CAS writes for a `DescUpdate`.

    `readme_bytes`/`desc.readme` are always both set on any `DescUpdate`
    `core/desc.py`'s `check_desc_change` returns (it raises `ValueError`
    itself rather than ever return one without a markdown layer) — narrowed
    with `cast` rather than an `if`, since that guard's false arm is
    unreachable given that contract and would otherwise be an uncoverable
    branch under this project's 100%-branch gate. `logo_bytes`/`desc.logo`
    are genuinely optional (no logo layer published) and get a real `if`.

    `DescUpdate` carries no layer media type (CONTRACTS.md §7's frozen shape
    does not expose one), so the logo extension is recovered by sniffing the
    PNG magic bytes — `core/desc.py` only ever populates `logo_bytes` for
    `image/png` or `image/svg+xml` layers, so "not PNG" -> "svg" is exhaustive
    for this module's input, not a heuristic guess. Flagged in
    `open_questions`: the robust fix is threading the media type through
    `DescUpdate` itself, out of this work package's scope (`core/desc.py` is
    WP2-B's file).
    """
    namespace, package = package_id.namespace, package_id.package
    readme_path = cas_relpath(namespace, package, cast(str, update.desc.readme), "md")
    writes: dict[str, bytes] = {readme_path: cast(bytes, update.readme_bytes)}
    if update.logo_bytes is not None:
        ext = "png" if update.logo_bytes.startswith(_PNG_MAGIC) else "svg"
        logo_path = cas_relpath(namespace, package, cast(str, update.desc.logo), ext)
        writes[logo_path] = update.logo_bytes
    return writes


def _reconcile_one(
    package_id: PackageId, *, files: FilePort, registry: RegistryPort, clock: ClockPort
) -> _PackageOutcome | None:
    """One package's sweep step. `None` if the root vanished between
    `list_files` and this read — the same race `core/observe.py` tolerates
    for an individual tag, extended to a whole root (not fatal)."""
    raw = files.read_bytes(_root_path(package_id))
    if raw is None:
        return None
    current = parse_package_root(raw)

    # SSRF ordering (G-03, ADR-4 BD-1): must run before any RegistryPort
    # call below — matches announce.py/validate.py's identical re-check of
    # this same trust boundary on a `PackageRoot` read back from the repo.
    check_repository_allowlisted(current.repository)
    check_repository_shape(current.repository)

    observations = observe(current.repository, registry)
    findings = check_tag_mutations(package_id, current, observations)
    if findings:
        return _PackageOutcome(findings=findings, patch=None, cas_writes={})

    desc_update = check_desc_change(current.repository, current.desc, registry)
    new_desc = current.desc if desc_update is None else desc_update.desc
    cas_writes = {} if desc_update is None else _desc_cas_writes(package_id, desc_update)

    target = regenerate(current, observations, new_desc, clock)
    patch = diff(package_id, current, target, observations)
    if patch is not None:
        for digest, obj in patch.new_objects:
            path = cas_relpath(package_id.namespace, package_id.package, digest, "json")
            cas_writes[path] = serialize_observation_object(obj)
    return _PackageOutcome(findings=(), patch=patch, cas_writes=cas_writes)


def _open_pr(patches: list[Patch], cas_writes: dict[str, bytes], github: GitHubPort) -> int:
    """Batch every clean-subset patch into one commit + one PR on the shared,
    idempotently-reused `_RECONCILE_BRANCH` (CONTRACTS.md §12)."""
    files: dict[str, bytes | None] = dict(cas_writes)
    for patch in patches:
        namespace, package = patch.package_id.namespace, patch.package_id.package
        files[f"{_ROOT_PREFIX}{namespace}/{package}.json"] = serialize_package_root(patch.root)

    base_sha = github.get_ref_sha(_RECONCILE_BRANCH) or github.get_ref_sha(_BASE_BRANCH)
    if base_sha is None:
        raise RuntimeError(f"base branch {_BASE_BRANCH!r} does not exist")

    github.commit_files(
        branch=_RECONCILE_BRANCH,
        base_sha=base_sha,
        message="indexbot reconcile: refresh from registry truth",
        files=files,
    )
    body = "\n".join(f"- {patch.package_id}: {patch.summary}" for patch in patches)
    return github.open_or_update_pull_request(
        branch=_RECONCILE_BRANCH,
        base=_BASE_BRANCH,
        title="indexbot reconcile: refresh from registry truth",
        body=body,
    )


def _build_summary(
    *,
    patch_count: int,
    finding_count: int,
    dry_run: bool,
    pr_number: int | None,
) -> str:
    if patch_count == 0:
        clean = "no-op: 0 packages changed"
    elif dry_run:
        clean = f"dry-run: {patch_count} package(s) would update"
    else:
        clean = f"applied: {patch_count} package(s) updated (PR #{pr_number})"
    noun = "anomaly" if finding_count == 1 else "anomalies"
    return f"{clean}; {finding_count} {noun}"


def run(
    args: argparse.Namespace,
    *,
    files: FilePort,
    registry: RegistryPort,
    github: GitHubPort,
    clock: ClockPort,
) -> ExitCode:
    """Full-index sweep. `args.dry_run` (bool) and `args.package` (optional
    `<namespace>/<package>` scope string) are read if present; both default
    to their "do everything, for real" values when absent, so callers that
    don't set up an `--package`/`--dry-run` argparse option still work.

    Ports are explicit keyword arguments rather than constructed inside this
    module (functional core / imperative shell, CONTRACTS.md §0) — WP2-M's
    `cli/main.py` wiring supplies the real adapters; tests supply
    `tests/fakes`.
    """
    dry_run = bool(getattr(args, "dry_run", False))
    scope = _resolve_scope(args)
    package_ids = _discover_package_ids(files, scope=scope)

    patches: list[Patch] = []
    cas_writes: dict[str, bytes] = {}
    findings: list[AnomalyFinding] = []

    for package_id in package_ids:
        outcome = _reconcile_one(package_id, files=files, registry=registry, clock=clock)
        if outcome is None:
            continue
        if outcome.findings:
            findings.extend(outcome.findings)
            continue
        if outcome.patch is not None:
            patches.append(outcome.patch)
            cas_writes.update(outcome.cas_writes)

    pr_number: int | None = None
    if patches and not dry_run:
        pr_number = _open_pr(patches, cas_writes, github)

    summary = _build_summary(
        patch_count=len(patches), finding_count=len(findings), dry_run=dry_run, pr_number=pr_number
    )
    # Captured by reconcile.yml's `tee reconcile.log` -> becomes the anomaly issue body.
    print(summary)
    write_github_output("result", summary)

    if findings:
        detail = "; ".join(
            f"{finding.package_id} {finding.tag}: committed={finding.committed_content} "
            f"fresh={finding.fresh_content}"
            for finding in findings
        )
        raise AnomalyError(f"{summary} -- {detail}")
    return ExitCode.OK
