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
import re
import sys
from typing import cast

from ocx_indexbot.core.gitlab_invariants import check_gitlab, top_level_section
from ocx_indexbot.core.policy import FORGE_VALUES
from ocx_indexbot.core.workflow_invariants import Finding, check_workflows
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.ports import FilePort

DEFAULT_DIR = ".github/workflows"
DEFAULT_GITLAB_DIR = ".gitlab-ci"
_GITLAB_ROOT_FILE = ".gitlab-ci.yml"
# One `include:` list entry: `- local: x`, `- remote: x`, `- project: x`
# (paired with a sibling `file:` line this scan never needs to read — the
# `project:` key alone is enough to know the form), `- template: x`. Matches
# equally against the single-mapping spelling (`include:\n  local: x`, no
# leading `-`).
_INCLUDE_ENTRY_RE = re.compile(r"^[ \t]*-?[ \t]*(local|remote|project|template):[ \t]*(.*)$")


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


def _load_local_actions(files: FilePort, directory: str) -> dict[str, str]:
    """Every local composite action's `action.yml`, keyed by the `uses:` path
    a workflow would name it with (`./.github/actions/<name>`).

    Sibling of the workflow directory, not inside it: GitHub reads workflows
    from `.github/workflows/` and actions from `.github/actions/`. WF-08
    follows these because a composite action's `run:` steps execute in the
    calling job, with the calling job's token — a resolver moved one file down
    is the same hole, and `ci.setup` renders exactly such a `uses:` step into
    the credentialed job.

    An action nested deeper (`.github/actions/<name>/<sub>/action.yml`) is
    keyed by its own directory, which is what a `uses:` would have to name.
    """
    parent = directory.rstrip("/").rpartition("/")[0]
    if not parent:
        return {}
    loaded: dict[str, str] = {}
    for path in files.list_files(f"{parent}/actions"):
        if not path.endswith(("/action.yml", "/action.yaml")):
            continue
        text = files.read_text(path)
        if text is not None:
            loaded[f"./{path.rpartition('/')[0]}"] = text
    return loaded


def _local_include_target(value: str) -> str:
    """A `local:` value, comment and quotes stripped, its GitLab-optional
    leading `/` removed.

    GitLab documents `templates/x.yml` and `/templates/x.yml` as identical —
    both relative to the project root — so treating the slash as an
    OS-absolute path would reject the commonly-recommended spelling. A
    genuine traversal attempt (`../../etc/passwd`) is unaffected: only one
    leading `/` is ever stripped, and what is left still goes through
    `FilePort.read_text`, which rejects `..` and an absolute path itself —
    the same untrusted-path discipline every other read in this package
    uses, not a bespoke check here.
    """
    return _strip_comment(value).strip("'\"").removeprefix("/")


def _strip_comment(value: str) -> str:
    return value.partition(" #")[0].strip()


def _include_local_targets(text: str) -> list[str]:
    """Every `local:` include target `text`'s top-level `include:` block
    names.

    GitLab accepts a bare scalar (`include: 'ci/jobs.yml'`) as shorthand for
    one local include, or one or more mapping entries — `- local: …`,
    `- project: …` (with a sibling `file:`), `- remote: …`, `- template: …`.
    Only `local:` names a file this checkout actually holds; the other three
    point somewhere this static audit cannot read at all (another project, an
    arbitrary URL, a GitLab-bundled template). Reporting a clean audit over a
    pipeline whose real job definitions live behind one of those is the bug
    this loader exists to close, so any of the three raises rather than being
    silently skipped.
    """
    section = top_level_section(text, "include")
    if not section:
        return []
    lines = section.splitlines()
    header_value = lines[0].partition(":")[2].strip()
    if len(lines) == 1:
        return [_local_include_target(header_value)] if header_value else []
    targets: list[str] = []
    for line in lines[1:]:
        match = _INCLUDE_ENTRY_RE.match(line)
        if match is None:
            continue
        kind, value = match.groups()
        if kind != "local":
            raise ValidationError(
                f"include: `{kind}:` cannot be followed by a static audit — this "
                "loader only reads `local:` includes from the checkout"
            )
        targets.append(_local_include_target(value))
    return targets


def _load_gitlab_pipeline(files: FilePort, directory: str) -> dict[str, str]:
    """The root `.gitlab-ci.yml`, every `*.yml`/`*.yaml` under `directory`,
    and every file `include:` names anywhere in that set — recursively, since
    an included file may itself `include:` further ones — keyed by their path
    from the checkout root.

    The directory walk and the `include:` walk are both kept: unlike GitHub's
    workflow directory, a GitLab `include: - local:` can name a file nested at
    any depth, or outside `directory` entirely (a hand-written pipeline
    conventionally puts its own job files at the repository root, not under
    `.gitlab-ci/`), so neither alone would see everything a real pipeline
    reads. An `include:` this loader cannot follow, or a `local:` target that
    does not resolve to a real file, raises `ValidationError` — see
    `_include_local_targets` — rather than letting `check_gitlab` report a
    clean audit over a tree it never read.
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

    pending = list(loaded.values())
    while pending:
        for target in _include_local_targets(pending.pop()):
            if target in loaded:
                continue
            included = files.read_text(target)
            if included is None:
                raise ValidationError(
                    f"include: local: {target!r} does not resolve to a file in this checkout"
                )
            loaded[target] = included
            pending.append(included)
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
        findings = check_workflows(
            pipeline, owner=owner, actions=_load_local_actions(files, directory)
        )
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
