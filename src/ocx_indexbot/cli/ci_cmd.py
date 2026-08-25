"""`indexbot ci` — render this index's pipeline files, or check them for drift.

Two modes over one render:

- **write** (default): render and write every file this deployment's policy
  calls for.
- **`--check`**: render, compare, write nothing, and exit non-zero on any
  difference. This is the gate. Generated files carry a governance argument
  the operator did not write and is not expected to re-derive — the trigger
  the privileged half may use, what it may check out, which pathspec selects a
  package root — so a hand-edit that survives is a security argument silently
  replaced by an opinion. It also fails on an *orphaned* generated file — one
  that still carries this tool's header but is no longer in the plan, the
  shape a `ci.forge` flip leaves behind (`build_render_plan` only plans one
  forge's file set, so the other forge's committed files simply drop out of
  scope). A workflow does not stop running because policy stopped planning
  it, so the gate must not go quiet exactly when it stops watching a file.

A file whose header names a *newer* tool version is refused in both modes: the
installed bot is older than whatever last rendered it, and quietly downgrading
a pipeline is worse than doing nothing. A file with no header at all is not
ours; it is overwritten on write and reported as drift on check, which is
exactly what the first run against a hand-written tree should do.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, cast

from ocx_indexbot.ci.render import (
    GITHUB_DIR,
    GITLAB_FILE,
    TOOL_HEADER_VERSION,
    build_render_plan,
    normalize_for_drift,
    parse_header_version,
)
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode

if TYPE_CHECKING:
    import argparse

    from ocx_indexbot.core.policy import IndexPolicy
    from ocx_indexbot.ports import FilePort


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift against the committed files and write nothing",
    )


def _refuse_newer_headers(existing: dict[str, str]) -> None:
    for path, text in existing.items():
        version = parse_header_version(text.split("\n", 1)[0])
        if version is not None and version > TOOL_HEADER_VERSION:
            raise ValidationError(
                f"{path} was rendered by indexbot ci v{version}, newer than this bot's "
                f"v{TOOL_HEADER_VERSION}. Upgrade ocx-indexbot rather than letting an older "
                "tool rewrite a pipeline it cannot fully read."
            )


def _orphaned_generated_files(files: FilePort, plan: dict[str, str]) -> list[str]:
    """Committed files that carry this tool's header but no longer appear in
    `plan` — the shape a `ci.forge` flip leaves behind. `build_render_plan`
    plans exactly one forge's file set, so flipping `github` -> `gitlab`
    drops the five GitHub workflow paths out of `plan` (and `existing`, which
    is scoped to `plan`'s own keys) without anyone deleting the files
    themselves: they stay committed, still generated, still running, just
    outside the comparison above.

    Scans both forges' possible locations regardless of which one is
    currently configured, since either direction of the flip leaves an
    orphan behind. A file is only reported if it still carries the header —
    a hand-written workflow living beside the generated ones is not this
    tool's problem.
    """
    candidates = [*files.list_files(GITHUB_DIR), GITLAB_FILE]
    orphans: list[str] = []
    for path in candidates:
        if path in plan:
            continue
        text = files.read_text(path)
        if text is not None and parse_header_version(text.split("\n", 1)[0]) is not None:
            orphans.append(path)
    return orphans


def run(args: argparse.Namespace, *, files: FilePort, policy: IndexPolicy) -> ExitCode:
    """`indexbot ci [--check]` entry point."""
    plan = build_render_plan(policy, existing={})
    existing = {path: text for path in plan if (text := files.read_text(path)) is not None}
    _refuse_newer_headers(existing)

    # Rendered a second time, now that the committed pins are known. The first
    # pass exists only to learn which paths to read; nothing from it is kept.
    plan = build_render_plan(policy, existing=existing)

    if not cast(bool, args.check):
        for path, content in plan.items():
            files.write_text(path, content)
        return ExitCode.OK

    drifted = [
        path
        for path, content in plan.items()
        if normalize_for_drift(existing.get(path, "")) != normalize_for_drift(content)
    ]
    orphaned = _orphaned_generated_files(files, plan)
    if not drifted and not orphaned:
        return ExitCode.OK
    for path in drifted:
        verb = "is missing" if path not in existing else "does not match what indexbot ci renders"
        print(f"indexbot ci: {path} {verb}", file=sys.stderr)
    for path in orphaned:
        print(
            f"indexbot ci: {path} still carries a generated header but ci.forge no longer "
            "plans it — its trigger is still live; delete the file",
            file=sys.stderr,
        )
    if drifted:
        print("indexbot ci: run `indexbot ci` and commit the result", file=sys.stderr)
    return ExitCode.VALIDATION_FAILURE
