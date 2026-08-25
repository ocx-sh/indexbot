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
(`ForgePort.get_file_contents`, always via `index_github` — read-only,
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
`head_repo` set to the fork.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final, cast

from ocx_indexbot.core.desc import check_desc_change
from ocx_indexbot.core.observe import observe_one_tag
from ocx_indexbot.core.policy import FORGE_VALUES, IndexPolicy
from ocx_indexbot.core.regenerate import regenerate
from ocx_indexbot.core.validate_entry import (
    TAG_NAME_RE,
    check_repository_allowlisted,
    parse_package_id,
    parse_package_root,
    serialize_package_root,
)
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import Yank

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterator

    from ocx_indexbot.core.observe import Observation
    from ocx_indexbot.model import PackageId, PackageRoot
    from ocx_indexbot.ports import ClockPort, FilePort, ForgePort, RegistryPort

BASE_REF: Final[str] = "main"
"""`--base-ref`'s default, and `cli/_wiring._base_ref`'s last fallback — what
the privileged subcommands read this deployment's policy file at when the
runner names no target branch, which is every scheduled lane.
`.github/index-policy.json` names an owner and a forge but never a
default-branch name — GitLab and a corporate GitHub org alike are free to
call it something other than `main` — so the per-run override is a CLI flag
a publisher passes, not a policy field: `announce` is a local tool invoked
per run, the same reason `--index-repo` and `--forge` are flags here rather
than config. This constant is only the fallback both places share."""
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
        "--index-repo",
        required=True,
        help="the index repository to announce into — <owner>/<repo> on GitHub, a "
        "namespace path or numeric project id on GitLab",
    )
    parser.add_argument(
        "--forge",
        choices=sorted(FORGE_VALUES),
        default=None,
        help="which forge hosts --index-repo and --fork; defaults to the CI runner's "
        "own signal, then to github",
    )
    parser.add_argument(
        "--base-ref",
        default=BASE_REF,
        help=f"the index repository's default branch (default: {BASE_REF!r}) — the base "
        "every announce PR opens against",
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
    return f"p/{package_id}.json"


def _cas_path(package_id: PackageId, digest: str, extension: str) -> str:
    hex_digest = digest.removeprefix("sha256:")
    return f"p/{package_id}/o/sha256/{hex_digest}.{extension}"


def _branch_name(package_id: PackageId) -> str:
    return "indexbot-announce-" + "-".join(package_id.segments)


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
    tags = tuple(_tag_tokens(raw))
    if not tags:
        raise ValidationError("no tags given (--tags/--tags-file was empty)")
    # `observe_one_tag` below hands `tag` straight to `RegistryPort.get_manifest`,
    # which builds a registry URL from it — `parse_package_root`'s A-5 grammar
    # check guards the *committed* side of that call (a tag already in a root),
    # but this is the earlier, uncommitted side: a curated tag never passes
    # through `parse_package_root` before reaching the registry. Percent-encoding
    # in the adapter and `validate`'s pre-merge check both still hold, so a bad
    # tag here is loud-later rather than a hole — but the grammar belongs at the
    # boundary where untrusted input becomes a registry call, not two steps past
    # it. Same constant as the committed-side check (`core/validate_entry`).
    bad = sorted(tag for tag in tags if TAG_NAME_RE.fullmatch(tag) is None)
    if bad:
        raise ValidationError(
            "tag name(s) do not match the OCI distribution tag grammar "
            "(schema/root.schema.json's tags.propertyNames.pattern): "
            + ", ".join(repr(tag) for tag in bad)
        )
    return tags


def _tag_tokens(raw: str) -> Iterator[str]:
    """The tags in a `--tags` string or a `--tags-file`, comments dropped.

    A tags file is a file a human maintains, so it grows `#` comments —
    "# the curated set" — and without this every one of them was parsed as a
    tag and failed with "does not resolve … check for a typo", which points
    at the wrong thing entirely. Comments are line-scoped: `#` after a comma
    on a shared line ends that line, and a tag may not contain `#` anyway.
    """
    for line in raw.splitlines():
        body = line.split("#", 1)[0]
        for part in body.split(","):
            token = part.strip()
            if token:
                yield token


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
    index_github: ForgePort,
    fork_github: ForgePort | None,
    files: FilePort,
    clock: ClockPort,
    policy: IndexPolicy,
) -> ExitCode:
    """`indexbot announce` entry point. See module docstring for the
    pipeline. `fork_github` is `None` for `--out` mode (never touched on that
    path) and required (non-`None`) for `--fork` mode."""
    package_id = parse_package_id(cast(str, args.package), name_segments=policy.name_segments)
    curated_tags = _resolve_curated_tags(args, files=files)
    yank = tuple(cast("list[str]", args.yank))
    unyank = tuple(cast("list[str]", args.unyank))
    yank_reason = cast(str, args.yank_reason)
    base_ref = cast(str, args.base_ref)

    root_path = _root_path(package_id)
    current_raw = index_github.get_file_contents(root_path, base_ref)
    if current_raw is None:
        raise ValidationError(
            f"unclaimed namespace: no committed root at {root_path!r} on {base_ref!r} for "
            f"{package_id} — new packages go through the human lane"
        )
    current = parse_package_root(current_raw)

    # BD-1 SSRF ordering: must run before any RegistryPort call below.
    check_repository_allowlisted(current.repository, policy.registry_hosts)

    observations: list[Observation] = []
    for tag in curated_tags:
        observation = observe_one_tag(current.repository, tag, registry)
        if observation is None:
            raise ValidationError(
                f"tag {tag!r} does not resolve on {current.repository!r} — check for a typo"
            )
        observations.append(observation)

    desc_update = check_desc_change(current.repository, current.desc, registry, name=current.name)
    new_desc = current.desc if desc_update is None else desc_update.desc

    target = regenerate(current, tuple(observations), new_desc, clock)
    target = _apply_yank_markers(target, yank=yank, unyank=unyank, reason=yank_reason, clock=clock)

    files_by_path: dict[str, bytes] = {root_path: serialize_package_root(target)}
    for observation in observations:
        files_by_path[_cas_path(package_id, observation.content_digest, "json")] = observation.raw
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

    # Unchanged => no-op: a byte-identical root already implies no new
    # image-index CAS (any tag-map/digest change would change the root
    # bytes), and `desc_update is None` rules out any new desc/readme/logo
    # CAS — so byte-equality plus no desc change means there is nothing to
    # write. Skip before either the `--out` or `--fork` write branch. Reuse
    # the already-serialized root bytes; do not serialize twice.
    # ponytail: compares against the index-repo `main` root only. This
    # reference tool deliberately does NOT implement the Rust client's C6
    # amendment F1 (fork-mode ensure-open-PR when the fork branch is ahead of
    # base) — a bounded, documented divergence (register FP-9 pattern); Track A
    # stays the reference for F1.
    target_raw = files_by_path[root_path]
    if target_raw == current_raw and desc_update is None:
        print("unchanged, nothing to announce")
        return ExitCode.OK

    out_dir = cast("str | None", args.out)
    if out_dir is not None:
        for path, content in files_by_path.items():
            files.write_bytes(f"{out_dir}/{path}", content)
        return ExitCode.OK

    fork = cast(str, args.fork)
    index_repo = cast(str, args.index_repo)
    branch = _branch_name(package_id)
    github = cast("ForgePort", fork_github)
    # Root content above is generated from UPSTREAM index main
    # (`index_github.get_file_contents(root_path, base_ref)`) + live registry
    # truth, so a fresh announce branch must be cut from upstream's main tip,
    # not the fork's — a stale fork main would produce a stale merge-base and
    # risk a spurious CONFLICTING PR against any concurrent upstream change to
    # the same root. Fork networks share object storage, so creating a fork
    # ref at an upstream SHA works. An already-open announce branch (on the
    # fork) is reused as-is — its own tip is the right base for a second
    # commit on the same PR.
    #
    # `base_repo` is what makes that true on a forge where it is not free.
    # A GitLab fork shares no object storage with its upstream — it answers
    # 404 for the upstream tip — so the fork has to be told which project the
    # base SHA lives in. It is passed only for a FRESH branch: an existing
    # announce branch's own tip is already in the fork.
    existing_tip = github.get_ref_sha(branch)
    base_sha = existing_tip or index_github.get_ref_sha(base_ref)
    if base_sha is None:
        raise ValidationError(f"base ref {base_ref!r} does not exist on {index_repo!r}")

    commit_files: dict[str, bytes | None] = dict(files_by_path)
    github.commit_files(
        branch=branch,
        base_sha=base_sha,
        message=f"announce: curate {package_id}",
        files=commit_files,
        base_repo=None if existing_tip else index_repo,
    )
    index_github.open_or_update_pull_request(
        branch=branch,
        base=base_ref,
        title=f"announce: curate {package_id}",
        body=f"Publisher-curated tag update for `{package_id}`.",
        head_repo=fork,
    )
    return ExitCode.OK
