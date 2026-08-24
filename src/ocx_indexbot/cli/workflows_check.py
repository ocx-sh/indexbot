# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""`indexbot workflows-check` — audit an index repository's workflow tree.

The invariants live in `core/workflow_invariants.py`; this module is the
argparse + FilePort shell around them. It is the one subcommand that reads a
repository's CI configuration rather than its index data, and the one an index
repo runs against itself in CI.

Exit codes are the pinned four: `0` when every invariant holds, `1`
(`VALIDATION_FAILURE`) when any does not. An empty directory is a failure, not
a pass — a check that silently audits nothing is the shape a required check
takes when its path is wrong.
"""

from __future__ import annotations

import argparse
import sys
from typing import cast

from ocx_indexbot.core.workflow_invariants import Finding, check_workflows
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.ports import FilePort

DEFAULT_DIR = ".github/workflows"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir",
        default=DEFAULT_DIR,
        help=f"workflow directory to audit, relative to the checkout (default: {DEFAULT_DIR})",
    )
    parser.add_argument(
        "--owner",
        default=None,
        help=(
            "repository owner login; enables the cron upstream-guard check "
            "(WF-07). Omit to skip that one invariant."
        ),
    )


def _load_workflows(files: FilePort, directory: str) -> dict[str, str]:
    """Every `*.yml`/`*.yaml` directly under `directory`, keyed by basename.

    `FilePort.list_files` walks recursively; workflow files are only ever read
    by GitHub at the top level of the directory, so nested paths (an
    `actions/**` composite tree under the same prefix) are skipped rather than
    audited under rules written for workflows.
    """
    prefix = directory.rstrip("/")
    loaded: dict[str, str] = {}
    for path in files.list_files(prefix):
        relative = path[len(prefix) :].lstrip("/")
        if "/" in relative or not relative.endswith((".yml", ".yaml")):
            continue
        text = files.read_text(path)
        if text is not None:
            loaded[relative] = text
    return loaded


def run(args: argparse.Namespace, *, files: FilePort) -> ExitCode:
    """`indexbot workflows-check` entry point. Expected `args` attributes:
    `dir` (str), `owner` (str | None).
    """
    directory = cast(str, args.dir)
    owner = cast("str | None", args.owner)

    workflows = _load_workflows(files, directory)
    if not workflows:
        raise ValidationError(f"no workflow files found under {directory!r}")

    findings: tuple[Finding, ...] = check_workflows(workflows, owner=owner)
    if findings:
        for finding in findings:
            print(str(finding), file=sys.stderr)
        raise ValidationError(
            f"{len(findings)} workflow invariant violation(s) across "
            f"{len(workflows)} audited workflow(s) in {directory!r}"
        )

    print(f"workflows-check: {len(workflows)} workflow(s) audited, no findings", file=sys.stderr)
    return ExitCode.OK
