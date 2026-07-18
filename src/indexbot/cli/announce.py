"""`indexbot announce` — publisher reference tool (fork-PR announce revamp,
owner-confirmed decision set 2026-07-18).

Publishers curate their own package's `tags` map and open a PR from their own
fork under their own GitHub identity — no index-side credentials, no
`repository_dispatch` doorbell, no `--validate-only` unprivileged job. This
module is the reference implementation of that publisher-side step: build one
package's root + CAS objects from live registry truth for a *curated* tag
set, then either write them locally (`--out`, for local review or a
publisher's own commit tooling) or open a fork-PR against the index repo
directly (`--fork`). Server-side privileged verification — does the PR
author's `github_id` actually own this package (G-19,
`cli/governance_check.py`), do the claims actually re-derive from registry
truth (`cli/validate.py`'s `core/verify_claims.py` wiring) — happens in CI,
never here.

Pipeline: resolve the curated tag set (`--tags`/`--tags-file`) -> read the
current committed root from the index repo at `main`
(`GitHubPort.get_file_contents`, always via `index_github` — read-only,
unauthenticated for `--out`) -> missing root -> `ValidationError`,
"unclaimed namespace — new packages go through the human lane" ->
`check_repository_allowlisted` (SSRF ordering, before any `RegistryPort`
call) -> `observe_one_tag` once per curated tag (a tag that does not resolve
is a hard `ValidationError` — a publisher typo, never silently dropped) ->
`check_desc_change` -> `regenerate` (owner curation: the observed set *is*
the new `tags` map — `core/regenerate.py`'s existing "observations are the
universe, absent means removed" semantics already gives exactly the curated
add/remove authority the decision set calls for, no core change needed) ->
`--yank`/`--unyank` marker toggles -> build root + CAS bytes -> `--out`:
write via `FilePort` under the wire paths; `--fork`: commit to a branch on
the fork repo (`fork_github`, scoped to `--fork`) and open/update a PR
against the index repo (`index_github`, scoped to `--index-repo`) with
`head_owner` set to the fork's owner.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final, cast

from indexbot.core.desc import check_desc_change
from indexbot.core.observe import observe_one_tag
from indexbot.core.regenerate import regenerate
from indexbot.core.validate_entry import (
    check_repository_allowlisted,
    parse_package_id,
    parse_package_root,
    serialize_observation_object,
    serialize_package_root,
)
from indexbot.errors import ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import Yank

if TYPE_CHECKING:
    import argparse

    from indexbot.core.observe import Observation
    from indexbot.model import PackageId, PackageRoot
    from indexbot.ports import ClockPort, FilePort, GitHubPort, RegistryPort

_BASE_REF: Final[str] = "main"
_DEFAULT_INDEX_REPO: Final[str] = "ocx-sh/index"
_DEFAULT_YANK_REASON: Final[str] = "yanked via announce"
_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `announce`'s CLI surface — a local publisher
    tool, not a CI doorbell target."""
    parser.add_argument("--package", required=True, help="<namespace>/<package> to announce")
    tags_group = parser.add_mutually_exclusive_group(required=True)
    tags_group.add_argument("--tags", default=None, help="comma-separated curated tag list")
    tags_group.add_argument(
        "--tags-file",
        default=None,
        help="local file of curated tags (comma- or newline-separated)",
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--out", default=None, help="write root + new CAS files locally under this directory"
    )
    target_group.add_argument(
        "--fork", default=None, help="<owner>/<repo> fork to commit to and open a PR from"
    )
    parser.add_argument(
        "--index-repo", default=_DEFAULT_INDEX_REPO, help="<owner>/<repo> of the index repository"
    )
    parser.add_argument(
        "--yank", action="append", default=[], metavar="TAG", help="mark TAG yanked"
    )
    parser.add_argument(
        "--unyank", action="append", default=[], metavar="TAG", help="clear TAG's yank marker"
    )
    parser.add_argument(
        "--yank-reason",
        default=_DEFAULT_YANK_REASON,
        help="reason recorded for every --yank in this run",
    )


def _root_path(package_id: PackageId) -> str:
    return f"p/{package_id.namespace}/{package_id.package}.json"


def _cas_path(package_id: PackageId, digest: str, extension: str) -> str:
    hex_digest = digest.removeprefix("sha256:")
    return f"p/{package_id.namespace}/{package_id.package}/o/sha256/{hex_digest}.{extension}"


def _branch_name(package_id: PackageId) -> str:
    return f"indexbot-announce-{package_id.namespace}-{package_id.package}"


def _logo_extension(data: bytes) -> str:
    """The two possible logo media types (`image/png`/`image/svg+xml`) are
    unambiguously distinguishable by the PNG magic number (`core/desc.py`'s
    `DescUpdate` carries no media-type/extension field)."""
    return "png" if data.startswith(_PNG_MAGIC) else "svg"


def _resolve_curated_tags(args: argparse.Namespace, *, files: FilePort) -> tuple[str, ...]:
    """The publisher's curated tag set from `--tags` or `--tags-file`
    (mutually exclusive at the argparse layer). `--tags-file` accepts either
    comma- or newline-separated tag names — read via `FilePort`, never a bare
    `open()`."""
    tags_arg = cast("str | None", getattr(args, "tags", None))
    if tags_arg is not None:
        raw = tags_arg
    else:
        tags_file = cast(str, args.tags_file)
        content = files.read_text(tags_file)
        if content is None:
            raise ValidationError(f"{tags_file!r} does not exist")
        raw = content
    tags = tuple(part.strip() for part in raw.replace("\n", ",").split(",") if part.strip())
    if not tags:
        raise ValidationError("no tags given (--tags/--tags-file was empty)")
    return tags


def _apply_yank_markers(
    root: PackageRoot,
    *,
    yank: tuple[str, ...],
    unyank: tuple[str, ...],
    reason: str,
    clock: ClockPort,
) -> PackageRoot:
    """Owner-curated yank/unyank (decision set: "yank != delete — yank is a
    marker that survives; delete is removal from the set"). Only applies to
    tags already present in the just-`regenerate`d curated set — a
    `--yank`/`--unyank` naming a tag outside that set, or naming the same tag
    in both lists, is a publisher-input error, never a silent no-op."""
    overlap = set(yank) & set(unyank)
    if overlap:
        raise ValidationError(f"tag(s) {sorted(overlap)} given to both --yank and --unyank")
    if not yank and not unyank:
        return root

    new_tags = dict(root.tags)
    for tag in yank:
        if tag not in new_tags:
            raise ValidationError(f"--yank {tag!r}: not in the curated tag set")
        new_tags[tag] = replace(new_tags[tag], yanked=Yank(reason=reason, at=clock.now_iso8601()))
    for tag in unyank:
        if tag not in new_tags:
            raise ValidationError(f"--unyank {tag!r}: not in the curated tag set")
        new_tags[tag] = replace(new_tags[tag], yanked=None)
    return replace(root, tags=new_tags)


def run(
    args: argparse.Namespace,
    *,
    registry: RegistryPort,
    index_github: GitHubPort,
    fork_github: GitHubPort | None,
    files: FilePort,
    clock: ClockPort,
) -> ExitCode:
    """`indexbot announce` entry point. See module docstring for the
    pipeline. `fork_github` is `None` for `--out` mode (never touched on that
    path) and required (non-`None`) for `--fork` mode."""
    package_id = parse_package_id(cast(str, args.package))
    curated_tags = _resolve_curated_tags(args, files=files)
    yank = tuple(cast("list[str]", args.yank))
    unyank = tuple(cast("list[str]", args.unyank))
    yank_reason = cast(str, args.yank_reason)

    root_path = _root_path(package_id)
    current_raw = index_github.get_file_contents(root_path, _BASE_REF)
    if current_raw is None:
        raise ValidationError(
            f"unclaimed namespace: no committed root at {root_path!r} on {_BASE_REF!r} for "
            f"{package_id} — new packages go through the human lane"
        )
    current = parse_package_root(current_raw)

    # BD-1 SSRF ordering: must run before any RegistryPort call below.
    check_repository_allowlisted(current.repository)

    observations: list[Observation] = []
    for tag in curated_tags:
        observation = observe_one_tag(current.repository, tag, registry)
        if observation is None:
            raise ValidationError(
                f"tag {tag!r} does not resolve on {current.repository!r} — check for a typo"
            )
        observations.append(observation)

    desc_update = check_desc_change(current.repository, current.desc, registry)
    new_desc = current.desc if desc_update is None else desc_update.desc

    target = regenerate(current, tuple(observations), new_desc, clock)
    target = _apply_yank_markers(target, yank=yank, unyank=unyank, reason=yank_reason, clock=clock)

    files_by_path: dict[str, bytes] = {root_path: serialize_package_root(target)}
    for observation in observations:
        files_by_path[_cas_path(package_id, observation.content_digest, "json")] = (
            serialize_observation_object(observation.object)
        )
    if desc_update is not None:
        # `check_desc_change` guarantees `readme_bytes`/`desc.readme` are
        # never `None` when it returns a `DescUpdate` (raises `ValueError`
        # itself otherwise) — cast, not a redundant runtime re-check.
        readme_digest = cast(str, desc_update.desc.readme)
        readme_bytes = cast(bytes, desc_update.readme_bytes)
        files_by_path[_cas_path(package_id, readme_digest, "md")] = readme_bytes
        if desc_update.logo_bytes is not None:
            logo_digest = cast(str, desc_update.desc.logo)
            files_by_path[
                _cas_path(package_id, logo_digest, _logo_extension(desc_update.logo_bytes))
            ] = desc_update.logo_bytes

    out_dir = cast("str | None", args.out)
    if out_dir is not None:
        for path, content in files_by_path.items():
            files.write_bytes(f"{out_dir}/{path}", content)
        return ExitCode.OK

    fork = cast(str, args.fork)
    fork_owner, _, _fork_repo = fork.partition("/")
    branch = _branch_name(package_id)
    github = cast("GitHubPort", fork_github)
    # ponytail: assumes the fork's default branch is also "main" (the common
    # GitHub fork convention) — upgrade to a `--fork-base` override if a
    # publisher's fork ever uses a different default branch name.
    base_sha = github.get_ref_sha(branch) or github.get_ref_sha(_BASE_REF)
    if base_sha is None:
        raise ValidationError(f"base ref {_BASE_REF!r} does not exist on {fork!r}")

    commit_files: dict[str, bytes | None] = dict(files_by_path)
    github.commit_files(
        branch=branch,
        base_sha=base_sha,
        message=f"announce: curate {package_id}",
        files=commit_files,
    )
    index_github.open_or_update_pull_request(
        branch=branch,
        base=_BASE_REF,
        title=f"announce: curate {package_id}",
        body=f"Publisher-curated tag update for `{package_id}`.",
        head_owner=fork_owner,
    )
    return ExitCode.OK
