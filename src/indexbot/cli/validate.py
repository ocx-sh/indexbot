"""`indexbot validate` — the unprivileged `schema-validate` PR-gate subcommand
(CONTRACTS.md §12).

Takes changed `p/<namespace>/<package>.json` root paths as CLI positional
args (the workflow's `git diff` step passes them — no `GitHubPort`, no
write-scoped token, this runs in a job with anonymous GHCR reads only).
Per changed root: every `core/validate_entry.py` structural check, then
(unless `--offline`) `core/registry_checks.py`'s two G-15 network checks
(digest-in-scope per observed platform manifest, ownership probe). Results
aggregate across every path given — one structured line per file (plus any
warnings) on stderr, one overall `ExitCode`.

Ports are required keyword-only arguments on `run` rather than the bare
`Callable[[argparse.Namespace], ExitCode]` shape CONTRACTS.md §12 quotes
verbatim — WP2-M's production wiring is expected to bind the real adapters
at `_DISPATCH` registration time (e.g.
`functools.partial(run, files=LocalFiles(...), registry=GhcrRegistry())`),
which still satisfies that single-argument dispatch shape once registered.
Flagged in this work package's `open_questions` in case a different binding
convention (e.g. a shared `Ports` bundle dataclass) was intended instead.

This module registers nothing in `cli/main.py` — `add_arguments` exists so
WP2-M's `subparsers.add_parser("validate")` wiring can populate the parser
without duplicating this subcommand's arg shape by hand.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from indexbot.core.registry_checks import check_digest_in_scope, check_ownership
from indexbot.core.validate_entry import (
    check_digest_self_consistent,
    check_name_matches_path,
    check_namespace_not_reserved,
    check_no_dangling_references,
    check_repository_allowlisted,
    check_repository_shape,
    check_superseded_by,
    check_tag_timestamps_z_anchored,
    check_upstream_repository_url_scheme,
    parse_digest,
    parse_observation_object,
    parse_package_id,
    parse_package_root,
    serialize_package_root,
)
from indexbot.core.verify_claims import verify_claims
from indexbot.errors import AnomalyError, ValidationError
from indexbot.exit_codes import ExitCode

if TYPE_CHECKING:
    import argparse

    from indexbot.model import PackageId
    from indexbot.ports import FilePort, RegistryPort


@dataclass(frozen=True, slots=True)
class FileReport:
    """One changed root's validation outcome — `run`'s structured per-file
    report unit.

    `exit_code` is `ExitCode.OK`, `ExitCode.VALIDATION_FAILURE`, or
    `ExitCode.ANOMALY` — never `ExitCode.TRANSIENT`, which propagates
    uncaught out of `run` instead of being aggregated (backoff-exhausted is
    a "give up entirely" signal, not a per-file outcome; see
    `core/observe.py`'s identical treatment, CONTRACTS.md §7).
    """

    path: str
    exit_code: ExitCode
    error: str | None = None
    warnings: tuple[str, ...] = ()


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `validate`'s CLI surface."""
    parser.add_argument("paths", nargs="+", help="changed p/<namespace>/<package>.json root paths")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip core/registry_checks.py's G-15 network checks (digest-in-scope, ownership)",
    )
    parser.add_argument(
        "--allow-reserved-namespace",
        action="store_true",
        help=(
            "admit OCX's own brand namespace segments (ocx, ocx-sh, ocx-contrib, ocx-rs) only "
            "— control-path and generic reserved segments (p, admin, ...) stay blocked"
        ),
    )


def _package_id_from_root_path(path: str) -> PackageId:
    """`p/<namespace>/<package>.json` -> a validated `PackageId`.

    Reuses `validate_entry.parse_package_id` for the namespace/package
    shape check (length caps + `PACKAGE_ID_RE`) rather than hand-rolling a
    second regex — the two-regex rule (ADR-4 BD-4) forbids sharing a
    *pattern* between the namespace/package shape and the OCI repository
    shape, not reusing this already-tested shape check for a second input
    that is meant to carry the same shape.
    """
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "p" or not parts[2].endswith(".json"):
        raise ValidationError(f"{path!r} is not a p/<namespace>/<package>.json root path")
    namespace, filename = parts[1], parts[2]
    package = filename.removesuffix(".json")
    return parse_package_id(f"{namespace}/{package}")


def _cas_prefix(namespace: str, package: str) -> str:
    return f"p/{namespace}/{package}/o/sha256/"


def _cas_paths_by_digest(files: FilePort, namespace: str, package: str) -> dict[str, str]:
    """digest string -> its CAS file path, for every file actually present
    under this package's `o/sha256/` tree. Built once per validated root so
    `desc.readme`/`desc.logo` byte lookups (extension unknown ahead of time,
    unlike a tag's always-`.json` object) don't re-list the same prefix per
    field, and so the byte-exact-discipline blanket hash-check below can walk
    every present file exactly once."""
    prefix = _cas_prefix(namespace, package)
    return {
        f"sha256:{file_path.rsplit('/', 1)[-1].split('.', 1)[0]}": file_path
        for file_path in files.list_files(prefix)
    }


def _validate_one(
    path: str, *, files: FilePort, registry: RegistryPort, offline: bool, allow_reserved: bool
) -> FileReport:
    """Runs the full structural pipeline for one changed root, catching
    `ValidationError`/`AnomalyError` into a `FileReport` (first failure wins
    — checks are guard-style, sequential, per CONTRACTS.md §5). A
    `TransientError` (backoff exhausted mid-registry-check) is deliberately
    not caught here — it propagates out of `run` uncaught.
    """
    warnings: list[str] = []
    try:
        raw = files.read_bytes(path)
        if raw is None:
            raise ValidationError(f"{path!r} does not exist")
        root = parse_package_root(raw)
        if serialize_package_root(root) != raw:
            # Byte-exact discipline (fork-PR announce revamp): CI re-derives
            # the canonical serialization of the PR's own parsed root and
            # byte-compares against the committed bytes — a root that
            # doesn't already sit in that exact canonical form is rejected
            # rather than silently re-canonicalized on merge.
            raise ValidationError(
                f"{path!r}: committed bytes are not the canonical root serialization "
                "(byte-exact discipline)"
            )
        package_id = _package_id_from_root_path(path)

        check_name_matches_path(package_id, root)
        check_superseded_by(root)
        check_upstream_repository_url_scheme(root)
        check_tag_timestamps_z_anchored(root)
        if allow_reserved:
            print(
                f"{path}: --allow-reserved-namespace used (brand-segment carve-out — "
                "control-path and generic segments still blocked)",
                file=sys.stderr,
            )
        check_namespace_not_reserved(package_id, allow_reserved=allow_reserved)
        # SSRF ordering (G-03, ADR-4 BD-1): must run before any RegistryPort
        # call reachable below.
        check_repository_allowlisted(root.repository)
        check_repository_shape(root.repository)

        for entry in root.tags.values():
            parse_digest(entry.content)
        if root.desc is not None:
            parse_digest(root.desc.digest)
            if root.desc.readme is not None:
                parse_digest(root.desc.readme)
            if root.desc.logo is not None:
                parse_digest(root.desc.logo)

        paths_by_digest = _cas_paths_by_digest(files, package_id.namespace, package_id.package)
        cas_digests = frozenset(paths_by_digest)
        check_no_dangling_references(root, cas_digests)

        # Byte-exact discipline (fork-PR announce revamp): hash-check EVERY
        # committed CAS file under this package's tree — tag objects and
        # desc readme/logo blobs alike, not only the ones a tag/desc field
        # happens to reference — so an orphaned or mislabeled CAS file is
        # caught too. Closes a real gap: previously only referenced tag
        # objects were ever byte-verified, desc blobs never were.
        for digest, cas_path in paths_by_digest.items():
            object_bytes = cast(bytes, files.read_bytes(cas_path))
            check_digest_self_consistent(digest, object_bytes)

        object_bytes_by_tag = {
            tag_name: cast(bytes, files.read_bytes(paths_by_digest[entry.content]))
            for tag_name, entry in root.tags.items()
        }

        if offline:
            warnings.append("G-15 registry checks skipped (--offline)")
        else:
            for tag_name in sorted(object_bytes_by_tag):
                observation_object = parse_observation_object(object_bytes_by_tag[tag_name])
                for platform_entry in observation_object.platforms:
                    # digest-hex fullmatch before it ever reaches a
                    # RegistryPort call (validate_entry.py's rule) — a CAS
                    # object's `platforms[*].digest` is PR-submitted content,
                    # not yet schema-validated at this point in the pipeline.
                    check_digest_in_scope(
                        root.repository, parse_digest(platform_entry.digest), registry
                    )
            ownership = check_ownership(root.repository, root.name, registry)
            if ownership == "mismatch":
                raise ValidationError(
                    f"{root.repository!r} ownership does not match {root.name!r} (G-15)"
                )
            if ownership == "unconfirmed":
                warnings.append("ownership unconfirmed (G-15) — WARN, not blocking (ADR-4 Risk 2)")

            # Fork-PR announce revamp: re-derive every claimed tag/desc-blob
            # digest from registry truth (subset semantics — verifies each
            # claim individually, never a full-set equality against the
            # registry's actual tag list, since owner curation legitimately
            # publishes a subset). A finding here means the PR's claim isn't
            # actually true right now — a validation failure for THIS PR,
            # not an anomaly against already-committed history (that's
            # `cli/reconcile.py`'s nightly-sweep disposition for the exact
            # same `verify_claims` findings).
            cas_bytes_by_digest = {
                entry.content: object_bytes_by_tag[tag_name]
                for tag_name, entry in root.tags.items()
            }
            if root.desc is not None:
                if root.desc.readme is not None:
                    cas_bytes_by_digest[root.desc.readme] = cast(
                        bytes, files.read_bytes(paths_by_digest[root.desc.readme])
                    )
                if root.desc.logo is not None:
                    cas_bytes_by_digest[root.desc.logo] = cast(
                        bytes, files.read_bytes(paths_by_digest[root.desc.logo])
                    )
            findings = verify_claims(package_id, root, cas_bytes_by_digest, registry)
            if findings:
                detail = "; ".join(f"{finding.kind}: {finding.detail}" for finding in findings)
                raise ValidationError(f"claim verification failed for {package_id}: {detail}")
    except AnomalyError as exc:
        return FileReport(
            path=path, exit_code=ExitCode.ANOMALY, error=str(exc), warnings=tuple(warnings)
        )
    except ValidationError as exc:
        return FileReport(
            path=path,
            exit_code=ExitCode.VALIDATION_FAILURE,
            error=str(exc),
            warnings=tuple(warnings),
        )
    return FileReport(path=path, exit_code=ExitCode.OK, warnings=tuple(warnings))


def _print_report(report: FileReport) -> None:
    if report.exit_code == ExitCode.OK:
        print(f"{report.path}: OK", file=sys.stderr)
    else:
        print(f"{report.path}: FAIL ({report.exit_code.name}) - {report.error}", file=sys.stderr)
    for warning in report.warnings:
        print(f"{report.path}: WARN - {warning}", file=sys.stderr)


def run(args: argparse.Namespace, *, files: FilePort, registry: RegistryPort) -> ExitCode:
    """`indexbot validate <path> [<path> ...] [--offline] [--allow-reserved-namespace]`
    (CONTRACTS.md §12).

    Aggregates every path's `FileReport`: prints one structured stderr line
    per file (plus warnings — a `--offline` run's skipped G-15 checks always
    surface as a WARN line, never a silent omission), then returns the worst
    `ExitCode` seen across all files (`OK` < `VALIDATION_FAILURE` <
    `ANOMALY` — this module's own escalation of CONTRACTS.md §12's "any
    ValidationError -> exit 1" to also cover the `AnomalyError`-tier
    `validate_entry` checks it runs, e.g. dangling CAS references or a
    tampered content digest; not explicitly resolved by CONTRACTS.md/either
    ADR, flagged in `open_questions`). `--allow-reserved-namespace` narrows
    `check_namespace_not_reserved` to OCX's own brand carve-out (mechanism
    only, policy PR-gated — same flag/semantics as `cli/seed_import.py`).
    """
    paths = cast(list[str], args.paths)
    offline = cast(bool, args.offline)
    allow_reserved = cast(bool, args.allow_reserved_namespace)

    reports = [
        _validate_one(
            path, files=files, registry=registry, offline=offline, allow_reserved=allow_reserved
        )
        for path in paths
    ]
    for report in reports:
        _print_report(report)

    return max((report.exit_code for report in reports), default=ExitCode.OK)
