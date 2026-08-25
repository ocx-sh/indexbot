"""`indexbot validate-pr` — the whole unprivileged pull-request gate as one
command.

It replaces three shell steps that every generated pipeline used to carry:
resolve the changed package roots, materialize their base-ref bytes, then run
`indexbot validate` with the right flags. All three are security controls, and
YAML is not testable — a `:(glob)` dropped from a pathspec, a `...` softened
to `..`, or a provenance comparison against the wrong variable are each a
silent disarming of the only check that stops a fork PR claiming a namespace
segment the index reserves for its own brand. Here they are Python with named
tests. `indexbot validate` stays, unchanged, for the caller who already knows
its file set.

Nothing here holds a token. This is the job that touches PR-head content
(ADR-4 BD-5's unprivileged half, ADR-6 FP-7), which is exactly why it holds no
credential — and why every input it cannot compute for itself has to come from
the runner's own environment rather than a forge API call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ocx_indexbot.cli import validate
from ocx_indexbot.cli._common import write_ci_annotation, write_ci_summary
from ocx_indexbot.core.policy import INDEX_POLICY_PATH, root_glob
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode

if TYPE_CHECKING:
    import argparse
    from collections.abc import Mapping

    from ocx_indexbot.core.policy import IndexPolicy
    from ocx_indexbot.ports import FilePort, GitPort, RegistryPort

_BASE_SHA_ENV: tuple[str, ...] = ("INDEXBOT_BASE_SHA", "CI_MERGE_REQUEST_DIFF_BASE_SHA")
"""Env vars naming the base commit, most explicit first.

`INDEXBOT_BASE_SHA` is the forge-independent escape hatch a hand-rolled
pipeline sets. `CI_MERGE_REQUEST_DIFF_BASE_SHA` is GitLab's own merge-base
variable, set on every merge-request pipeline. GitHub has no equivalent
variable, so it is handled separately below.
"""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `validate-pr`'s CLI surface."""
    parser.add_argument(
        "--base-sha",
        default=None,
        help=(
            "the pull request's base commit; defaults to $INDEXBOT_BASE_SHA, then "
            "$CI_MERGE_REQUEST_DIFF_BASE_SHA, then origin/$GITHUB_BASE_REF"
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip core/registry_checks.py's G-15 network checks (digest-in-scope, ownership)",
    )
    provenance = parser.add_mutually_exclusive_group()
    provenance.add_argument(
        "--same-repo-pr",
        action="store_true",
        help=(
            "this PR's head branch lives in the index repository itself — admits the "
            "operator's own reserved brand segments; overrides the environment sniff"
        ),
    )
    provenance.add_argument(
        "--fork-pr",
        action="store_true",
        help="this PR comes from a fork — reserved brand segments stay blocked",
    )


def _resolve_base_sha(explicit: str | None, env: Mapping[str, str]) -> str:
    """The commit to diff against, from the flag or the runner's own variables.

    `origin/$GITHUB_BASE_REF` is the GitHub fallback, and it needs
    `fetch-depth: 0` on the checkout — a shallow clone has no merge base to
    find, and `git` says so plainly, which is a better failure than silently
    diffing against nothing.
    """
    if explicit:
        return explicit
    for name in _BASE_SHA_ENV:
        value = env.get(name)
        if value:
            return value
    base_ref = env.get("GITHUB_BASE_REF")
    if base_ref:
        return f"origin/{base_ref}"
    raise ValidationError(
        "cannot determine the pull request's base commit: pass --base-sha, or set "
        "$INDEXBOT_BASE_SHA (any CI), $CI_MERGE_REQUEST_DIFF_BASE_SHA (GitLab) or "
        "$GITHUB_BASE_REF (GitHub, with fetch-depth: 0)"
    )


def _github_event_head_repo(event_path: str) -> str | None:
    """`pull_request.head.repo.full_name` from the GitHub event payload, or
    `None` when the payload does not have one.

    Walked key by key with an `isinstance` at every step because this file is
    JSON of a shape this process did not produce: a `push` event has no
    `pull_request` at all, and a pull request opened from a fork whose
    repository was since deleted carries `head.repo: null`. Both must read as
    "unknown", which the caller turns into "fork" — fail closed.
    """
    try:
        node: object = json.loads(Path(event_path).read_bytes())
    except (OSError, ValueError):
        return None
    for key in ("pull_request", "head", "repo", "full_name"):
        if not isinstance(node, dict):
            return None
        node = cast("dict[str, object]", node).get(key)
    return node if isinstance(node, str) else None


def _same_repo_pr(args: argparse.Namespace, env: Mapping[str, str]) -> bool:
    """Whether this pull request's head branch lives in the index repository
    itself — the provenance gate behind `--allow-reserved-namespace`.

    ADR-2 ND-4's reserved segments (`reserved_namespaces` in
    `.github/index-policy.json`) cannot host the operator's own first-party
    roots without that flag, but passing it unconditionally would hand every
    fork PR the right to claim those segments — to publish under the index's
    own brand and be believed by every client that resolves through this
    index. So the flag is withheld unless the PR is provably the index
    repository's own.

    This lane holds **no token**, so it cannot ask the forge API; the answer
    has to come from the runner's environment:

    - **GitHub** — `pull_request.head.repo.full_name` in `$GITHUB_EVENT_PATH`'s
      payload, compared against `$GITHUB_REPOSITORY`. Both are written by the
      runner, not by the PR author.
    - **GitLab** — `$CI_MERGE_REQUEST_SOURCE_PROJECT_PATH` against
      `$CI_MERGE_REQUEST_PROJECT_PATH`. **Not** `$CI_PROJECT_PATH`: that names
      the project the pipeline is *running in*, and a fork merge request's
      pipeline runs in the fork, so it equals the source project for every
      merge request, fork or not — the comparison would always be true.
      `$CI_MERGE_REQUEST_PROJECT_PATH` is the *target* project regardless of
      where the pipeline executes, which is what actually tells the two apart.
    - **Neither resolvable** — fork. Fail closed: an unrecognized environment
      must not be the thing that unlocks the brand.

    `--same-repo-pr`/`--fork-pr` win over all of it, for a pipeline this bot
    did not generate and whose variables it therefore cannot know. They are
    argparse-mutually-exclusive, so "both" is not expressible.

    The flag governs CLAIMING only. A fork PR that merely UPDATES a root
    already committed under a reserved segment — the `ocx package announce
    --fork` re-announce lane, which can open nothing but fork PRs — is
    admitted by the base-ref bytes below without the flag, and only while the
    change stays announce-shaped (`core/diff.classify_change` == "refresh").
    """
    if cast(bool, args.same_repo_pr):
        return True
    if cast(bool, args.fork_pr):
        return False
    event_path = env.get("GITHUB_EVENT_PATH")
    if event_path:
        head_repo = _github_event_head_repo(event_path)
        return head_repo is not None and head_repo == env.get("GITHUB_REPOSITORY")
    source = env.get("CI_MERGE_REQUEST_SOURCE_PROJECT_PATH")
    target = env.get("CI_MERGE_REQUEST_PROJECT_PATH")
    if source and target:
        return source == target
    return False


def _reject_head_authored_policy(base_sha: str, *, git: GitPort, files: FilePort) -> None:
    """Refuse to run at all when this pull request carries its own copy of
    `.github/index-policy.json`.

    This job checks out `pull_request.head.sha` — it has to, it is the half
    that reads PR-head content (ADR-4 BD-5, ADR-6 FP-7) — so the policy
    `cli/_wiring._local_policy` reads out of that checkout is whatever the
    **pull request** committed there. Every field in that file steers this
    gate: `name_segments` builds the `:(glob)` pathspec below that decides
    which paths get validated at all, `reserved_namespaces` names the brand
    segments ADR-2 ND-4 withholds from a fork, `registry_hosts` is G-03's
    allowlist. A fork declaring `"name_segments": 3` makes `root_glob` select
    `p/*/*/*.json`, its own two-segment `p/<reserved>/pkg.json` matches
    nothing, `run` prints "No package-root changes" and exits `0` — the
    required `schema-validate-pr` context green, having validated nothing.
    Emptying `reserved_namespaces` in the same file is the shorter spelling
    of the same move.

    That is contained, not harmless. The machine-merge lane cannot be reached
    this way — `cli/classify_pr.py` and `cli/governance_check.py` run under
    `_wiring._base_ref_policy` and force `human-review-required` on any
    changed path outside `p/**`, this file included — but a green required
    check is what a reviewer reads before pressing merge, and it would be a
    green nobody earned.

    **Byte equality, not a field-by-field comparison.** Deciding which keys
    "matter enough" to reject is exactly the judgment that goes wrong on the
    next key someone adds, and every key this file carries steers something
    here. Bytes need no such judgment and cannot be wrong in the unsafe
    direction. Once this returns, the `IndexPolicy` the wiring handed `run`
    is provably the base ref's own, so obeying it is obeying the base — which
    is what makes it safe to keep taking the policy from the checkout.

    **Before the diff, not after.** The pathspec that decides "this pull
    request changed no roots" is itself derived from the policy, so a check
    placed after `changed_package_roots` would let a head-authored
    `name_segments` reach the zero-root early exit first and return `0`
    before ever being examined.

    **A base ref with no policy file** is refused for the same reason rather
    than adopting the pull request's: an index whose base ref never committed
    one has no policy in force for this gate to obey, and inheriting the
    incoming branch's is precisely the trust direction this function exists
    to reverse. The file is deployment policy — it lands on the default
    branch, by the operator who owns it, never through a pull request the
    same file governs. The provenance flags do not enter into it:
    `--same-repo-pr`/`--fork-pr` say who opened the request, not which
    policy is in force.
    """
    head = files.read_bytes(INDEX_POLICY_PATH)
    base = git.file_at(base_sha, INDEX_POLICY_PATH)
    if head == base:
        return
    detail = (
        f"the base ref has no {INDEX_POLICY_PATH}"
        if base is None
        else f"{INDEX_POLICY_PATH} differs from its copy on the base ref"
    )
    raise ValidationError(
        f"{detail} ({base_sha}). indexbot validate-pr will not run under a policy the "
        f"pull request itself authored: {INDEX_POLICY_PATH} decides which paths this "
        "gate validates, which namespace segments it protects and which registry hosts "
        "it admits. It is the deployment's own policy and lands on the base branch, "
        "never through a pull request the same file governs."
    )


def _materialize_base(
    paths: tuple[str, ...], *, base_sha: str, git: GitPort, base_files: FilePort
) -> None:
    """Write each changed root's BASE-ref bytes into `base_files`' tree.

    ADR-2 ND-4 gates CLAIMING a reserved segment, not UPDATING a root already
    committed under one, and `validate` needs both versions to tell those
    apart (and to bound the exemption to an announce-shaped change).

    `base_files` is rooted **outside the workspace** — the PR-head tree is
    what `validate` byte-compares against its own canonical serialization and
    must stay exactly as checked out. A root that does not exist at the base
    ref is simply not written, so `validate` sees no base bytes for it and
    treats it as a new claim: fail closed.
    """
    for path in paths:
        raw = git.file_at(base_sha, path)
        if raw is not None:
            base_files.write_bytes(path, raw)


def run(
    args: argparse.Namespace,
    *,
    git: GitPort,
    files: FilePort,
    registry: RegistryPort,
    policy: IndexPolicy,
    base_files: FilePort,
) -> ExitCode:
    """`indexbot validate-pr [--base-sha SHA] [--offline] [--same-repo-pr | --fork-pr]`.

    Resolves the base commit, refuses a pull request that brought its own copy
    of the deployment policy (`_reject_head_authored_policy`), diffs the base
    three-dot against `HEAD` through a `:(glob)` pathspec built from the
    deployment's own `name_segments`, materializes each changed root's
    base-ref bytes, decides the reserved-namespace carve-out from PR
    provenance, and then runs exactly the validation `indexbot validate` runs
    — `cli/validate.validate_paths`, one implementation, so no rule can hold on
    one entry point and not the other.

    Exit codes are `validate`'s, unchanged (ADR-4 BD-2): `0` when every root
    passes *and* when the PR changed no root at all, `1` on a validation
    failure, `65` on an anomaly, `75` when registry backoff is exhausted (a
    `TransientError` propagating uncaught, mapped by `cli/main.py`).
    """
    base_sha = _resolve_base_sha(cast("str | None", args.base_sha), os.environ)
    _reject_head_authored_policy(base_sha, git=git, files=files)
    paths = git.changed_package_roots(base_sha, root_glob=root_glob(policy.name_segments))
    if not paths:
        write_ci_annotation(
            "notice",
            "indexbot validate-pr",
            "No package-root changes in this pull request - nothing to validate.",
        )
        return ExitCode.OK

    _materialize_base(paths, base_sha=base_sha, git=git, base_files=base_files)

    reports = validate.validate_paths(
        paths,
        files=files,
        registry=registry,
        policy=policy,
        offline=cast(bool, args.offline),
        allow_reserved=_same_repo_pr(args, os.environ),
        base_files=base_files,
    )
    for report in reports:
        validate.print_report(report)

    exit_code = validate.worst_exit_code(reports)
    if exit_code != ExitCode.OK:
        # The CLI's own structured stderr lines are the record; this is the
        # presentation the checks UI and the job-summary page surface. The
        # annotation carries only this fixed reason — every failing root's
        # message is derived from PR content, so it goes in the fenced block
        # `write_ci_summary` builds and nowhere else (ADR-4 BD-4).
        reason = (
            f"indexbot validate-pr rejected this pull request's package roots "
            f"(exit {int(exit_code)}). Details below."
        )
        write_ci_annotation("error", "indexbot validate-pr failed", reason)
        write_ci_summary(
            "indexbot validate-pr failed",
            reason,
            "\n".join(
                f"{report.path}: {report.exit_code.name} - {report.error}"
                for report in reports
                if report.error is not None
            ),
        )
    return exit_code
