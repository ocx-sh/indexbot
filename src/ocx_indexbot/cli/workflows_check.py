# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""`indexbot workflows-check` — audit an index repository's CI tree.

The invariants live in `core/workflow_invariants.py` (GitHub) and
`core/gitlab_invariants.py` (GitLab); this module is the argparse + FilePort
shell around both. It is the one subcommand that reads a repository's CI
configuration rather than its index data, and the one an index repo runs
against itself in CI.

Exit codes are the pinned four: `0` when every invariant holds, `1`
(`VALIDATION_FAILURE`) when any does not. An empty directory is a failure, not
a pass — a check that silently audits nothing is the shape a required check
takes when its path is wrong.
"""

from __future__ import annotations

import argparse
import sys
from typing import cast

from ocx_indexbot.core.gitlab_invariants import check_gitlab
from ocx_indexbot.core.policy import FORGE_VALUES
from ocx_indexbot.core.workflow_invariants import Finding, check_workflows
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.ports import FilePort

DEFAULT_DIR = ".github/workflows"
DEFAULT_GITLAB_DIR = ".gitlab-ci"
_GITLAB_ROOT_FILE = ".gitlab-ci.yml"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--forge",
        choices=sorted(FORGE_VALUES),
        default="github",
        help="which forge's CI tree to audit (default: github)",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help=(
            "directory to audit, relative to the checkout. On github: the "
            f"workflow directory (default: {DEFAULT_DIR}). On gitlab: the "
            f"included-file directory alongside the root {_GITLAB_ROOT_FILE} "
            f"(default: {DEFAULT_GITLAB_DIR})"
        ),
    )
    parser.add_argument(
        "--owner",
        default=None,
        help=(
            "repository owner login; enables the cron upstream-guard check "
            "(WF-07, github only). Omit to skip that one invariant."
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


def _load_gitlab_pipeline(files: FilePort, directory: str) -> dict[str, str]:
    """The root `.gitlab-ci.yml` plus every `*.yml`/`*.yaml` under `directory`,
    keyed by their path from the checkout root.

    Unlike GitHub's workflow directory, a GitLab `include: - local:` can name
    a file nested at any depth (`.gitlab-ci/jobs/build.yml`), so this walks
    the whole subtree rather than stopping at the top level.
    """
    loaded: dict[str, str] = {}
    root_text = files.read_text(_GITLAB_ROOT_FILE)
    if root_text is not None:
        loaded[_GITLAB_ROOT_FILE] = root_text
    for path in files.list_files(directory.rstrip("/")):
        if not path.endswith((".yml", ".yaml")):
            continue
        text = files.read_text(path)
        if text is not None:
            loaded[path] = text
    return loaded


def run(args: argparse.Namespace, *, files: FilePort) -> ExitCode:
    """`indexbot workflows-check` entry point. Expected `args` attributes:
    `dir` (str | None), `owner` (str | None), `forge` (str).
    """
    forge = cast(str, args.forge)
    directory = cast("str | None", args.dir)
    owner = cast("str | None", args.owner)

    if forge == "gitlab":
        directory = directory if directory is not None else DEFAULT_GITLAB_DIR
        pipeline = _load_gitlab_pipeline(files, directory)
        findings: tuple[Finding, ...] = check_gitlab(pipeline)
        noun = "pipeline"
    else:
        directory = directory if directory is not None else DEFAULT_DIR
        pipeline = _load_workflows(files, directory)
        findings = check_workflows(pipeline, owner=owner)
        noun = "workflow"

    if not pipeline:
        raise ValidationError(f"no {noun} files found under {directory!r}")

    if findings:
        for finding in findings:
            print(str(finding), file=sys.stderr)
        raise ValidationError(
            f"{len(findings)} {noun} invariant violation(s) across "
            f"{len(pipeline)} audited {noun}(s) in {directory!r}"
        )

    print(f"workflows-check: {len(pipeline)} {noun}(s) audited, no findings", file=sys.stderr)
    return ExitCode.OK
