"""`indexbot announce` — single-package regenerate + PR (CONTRACTS.md §12).

`repository_dispatch` doorbell target: the untrusted payload (`PACKAGE_ID`,
env-var-indirected per ADR-4 BD-4) is only ever used as a lookup key. Every
written field is re-derived from GHCR registry truth by `core/observe.py` /
`core/desc.py` — the payload can never lie about content (ADR-4's "announce
protocol", D4).

Pipeline: `validate_payload.parse_package_id` -> (stop here if
`--validate-only`) -> read the already-committed root at `main`
(`GitHubPort.get_file_contents`; missing -> `ValidationError`, "announce for
an unclaimed namespace") -> `check_repository_allowlisted` (BD-1 SSRF
ordering — must run before any `RegistryPort` call) -> `observe` ->
`desc.check_desc_change` -> `anomaly.check_tag_mutations` (any finding ->
`AnomalyError`, exit 65, **before** any write) -> `regenerate` -> `diff`
(`None` -> exit 0, `result=no-op`) -> `commit_files` +
`open_or_update_pull_request` + `add_labels` (via `diff.classify_change`,
reused verbatim — never reclassified by hand) + `enable_auto_merge` when
refresh-class -> `write_github_output("result", "applied")` + `pr_number`.

**Deviation from CONTRACTS.md §12's literal `run(args: argparse.Namespace) ->
ExitCode` shape — flagged here, not silently applied.** This module's `run`
additionally takes `registry`/`github`/`clock` as required keyword-only
ports, per this work package's explicit instruction ("Module-level
run(args, ports...) entry") and so tests can drive it against
`tests/fakes/` only, with no `adapters/` import in this file (ADR-4 BD-1
keeps `httpx` confined to `adapters/`; this module stays pure I/O-boundary
glue, not the place real adapters get constructed). WP2-M's `_DISPATCH`
wiring (`Callable[[argparse.Namespace], ExitCode]`) will need a small
closure/partial around `announce.run` to supply the real adapters — a
literal `_DISPATCH["announce"] = announce.run` assignment will not
type-check as-is. See `open_questions`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

from indexbot.core.anomaly import check_tag_mutations
from indexbot.core.desc import check_desc_change
from indexbot.core.diff import classify_change, diff
from indexbot.core.observe import observe
from indexbot.core.regenerate import regenerate
from indexbot.core.validate_entry import (
    check_repository_allowlisted,
    parse_package_root,
    serialize_observation_object,
    serialize_package_root,
)
from indexbot.core.validate_payload import PACKAGE_ID_MAX_LENGTH, PACKAGE_ID_RE, parse_package_id
from indexbot.errors import AnomalyError, ValidationError
from indexbot.exit_codes import ExitCode

from ._common import read_validated_env, write_github_output

if TYPE_CHECKING:
    import argparse

    from indexbot.model import PackageId
    from indexbot.ports import ClockPort, GitHubPort, RegistryPort

_BASE_REF: Final[str] = "main"
_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"


def _root_path(package_id: PackageId) -> str:
    return f"p/{package_id.namespace}/{package_id.package}.json"


def _cas_path(package_id: PackageId, digest: str, extension: str) -> str:
    hex_digest = digest.removeprefix("sha256:")
    return f"p/{package_id.namespace}/{package_id.package}/o/sha256/{hex_digest}.{extension}"


def _branch_name(package_id: PackageId) -> str:
    return f"indexbot-announce-{package_id.namespace}-{package_id.package}"


def _logo_extension(data: bytes) -> str:
    """`core/desc.py`'s `DescUpdate` carries no media-type/extension field
    (its own docstring flags this gap). The two possible logo media types
    (`image/png`/`image/svg+xml`) are unambiguously distinguishable by the
    PNG magic number, so the CAS extension is derived by sniffing the bytes
    rather than widening `DescUpdate`'s frozen shape (out of this work
    package's scope — see `open_questions`)."""
    return "png" if data.startswith(_PNG_MAGIC) else "svg"


def run(
    args: argparse.Namespace,
    *,
    registry: RegistryPort,
    github: GitHubPort,
    clock: ClockPort,
) -> ExitCode:
    """`indexbot announce` entry point. See module docstring for the pipeline.

    `args.package`: optional `<namespace>/<package>` override for manual/
    local invocation — bypasses the `PACKAGE_ID` env-var read entirely when
    set. The real `announce.yml` workflow never passes it (env-var
    indirection only, ADR-4 BD-4); it exists for local/manual dispatch.
    `args.validate_only`: stop immediately after payload-shape validation —
    no port is touched, matching `announce.yml`'s unprivileged
    `validate-payload` job ("no network, no write scope").
    """
    if args.package is not None:
        package_id = parse_package_id(args.package)
    else:
        raw = read_validated_env(
            "PACKAGE_ID", pattern=PACKAGE_ID_RE, max_length=PACKAGE_ID_MAX_LENGTH
        )
        package_id = parse_package_id(raw)

    if args.validate_only:
        write_github_output("result", "validated")
        return ExitCode.OK

    root_path = _root_path(package_id)
    current_raw = github.get_file_contents(root_path, _BASE_REF)
    if current_raw is None:
        raise ValidationError(
            f"announce for an unclaimed namespace: no committed root at {root_path!r} "
            f"on {_BASE_REF!r} for {package_id}"
        )
    current = parse_package_root(current_raw)

    # BD-1 SSRF ordering: must run before any RegistryPort call below.
    check_repository_allowlisted(current.repository)

    observations = observe(current.repository, registry)
    desc_update = check_desc_change(current.repository, current.desc, registry)

    findings = check_tag_mutations(package_id, current, observations)
    if findings:
        detail = "; ".join(
            f"{finding.tag}: committed {finding.committed_content} != fresh {finding.fresh_content}"
            for finding in findings
        )
        raise AnomalyError(f"registry content mutation detected for {package_id}: {detail}")

    new_desc = current.desc if desc_update is None else desc_update.desc
    target = regenerate(current, observations, new_desc, clock)
    patch = diff(package_id, current, target, observations)

    if patch is None:
        write_github_output("result", "no-op")
        return ExitCode.OK

    change_class = classify_change(current, target)

    files: dict[str, bytes | None] = {root_path: serialize_package_root(target)}
    for digest, observation_object in patch.new_objects:
        files[_cas_path(package_id, digest, "json")] = serialize_observation_object(
            observation_object
        )
    if desc_update is not None:
        # `check_desc_change` guarantees `readme_bytes`/`desc.readme` are
        # never `None` when it returns a `DescUpdate` (raises `ValueError`
        # itself otherwise, before ever returning) — `cast`, not a redundant
        # runtime re-check, documents that already-enforced invariant.
        readme_digest = cast(str, desc_update.desc.readme)
        readme_bytes = cast(bytes, desc_update.readme_bytes)
        files[_cas_path(package_id, readme_digest, "md")] = readme_bytes
        if desc_update.logo_bytes is not None:
            logo_digest = cast(str, desc_update.desc.logo)
            files[_cas_path(package_id, logo_digest, _logo_extension(desc_update.logo_bytes))] = (
                desc_update.logo_bytes
            )

    branch = _branch_name(package_id)
    base_sha = github.get_ref_sha(branch)
    if base_sha is None:
        base_sha = github.get_ref_sha(_BASE_REF)
        if base_sha is None:
            raise ValidationError(f"base ref {_BASE_REF!r} does not exist")

    github.commit_files(
        branch=branch,
        base_sha=base_sha,
        message=f"indexbot: regenerate {package_id}\n\n{patch.summary}",
        files=files,
    )
    pr_number = github.open_or_update_pull_request(
        branch=branch,
        base=_BASE_REF,
        title=f"indexbot: regenerate {package_id}",
        body=f"Automated regeneration for `{package_id}` ({change_class}).\n\n{patch.summary}",
    )
    github.add_labels(pr_number, [change_class])
    if change_class == "refresh":
        github.enable_auto_merge(pr_number)

    write_github_output("result", "applied")
    write_github_output("pr_number", str(pr_number))
    return ExitCode.OK
