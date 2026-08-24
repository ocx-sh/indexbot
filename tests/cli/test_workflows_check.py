# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""`indexbot workflows-check` — the FilePort shell around the invariants.

The invariants themselves are `tests/core/test_workflow_invariants.py`'s; what
is pinned here is what the subcommand reads (top-level `*.yml`/`*.yaml` under
the given prefix, nothing nested), what it refuses (an empty directory), and
where its output goes (findings on stderr, stdout untouched — the exit code is
the result).
"""

from __future__ import annotations

import argparse

import pytest

from ocx_indexbot.cli import workflows_check
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from tests.fakes import InMemoryFiles

_SHA = "b" * 40

_CLEAN = f"""\
name: ci

on:
  push:

permissions: {{}}

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{_SHA}
"""

_FLOATING = _CLEAN.replace(f"actions/checkout@{_SHA}", "actions/checkout@v4")


def _args(directory: str = ".github/workflows", owner: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(dir=directory, owner=owner)


def test_a_clean_tree_exits_ok_and_reports_the_count(capsys: pytest.CaptureFixture[str]) -> None:
    files = InMemoryFiles()
    files.write_text(".github/workflows/ci.yml", _CLEAN)
    files.write_text(".github/workflows/release.yaml", _CLEAN)

    assert workflows_check.run(_args(), files=files) == ExitCode.OK

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "2 workflow(s) audited, no findings" in captured.err


def test_a_violation_raises_validation_error_and_names_it_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = InMemoryFiles()
    files.write_text(".github/workflows/ci.yml", _FLOATING)

    with pytest.raises(ValidationError, match="1 workflow invariant violation"):
        workflows_check.run(_args(), files=files)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ci.yml: [WF-02]" in captured.err


def test_an_empty_directory_is_a_failure_not_a_pass() -> None:
    """A required check that silently audits nothing is the shape a wrong
    `--dir` takes: green, and asserting neither invariant."""
    with pytest.raises(ValidationError, match="no workflow files found"):
        workflows_check.run(_args(), files=InMemoryFiles())


def test_nested_paths_and_non_yaml_files_are_not_audited(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GitHub only reads workflows at the top level of the directory; a
    composite action nested under the same prefix is not a workflow, and would
    be judged by rules written for one."""
    files = InMemoryFiles()
    files.write_text(".github/workflows/ci.yml", _CLEAN)
    files.write_text(".github/workflows/actions/setup/action.yml", _FLOATING)
    files.write_text(".github/workflows/README.md", "not a workflow")

    assert workflows_check.run(_args(), files=files) == ExitCode.OK
    assert "1 workflow(s) audited" in capsys.readouterr().err


def test_a_trailing_slash_on_the_directory_is_accepted() -> None:
    files = InMemoryFiles()
    files.write_text(".github/workflows/ci.yml", _CLEAN)

    assert workflows_check.run(_args(".github/workflows/"), files=files) == ExitCode.OK


def test_owner_enables_the_cron_guard_check(capsys: pytest.CaptureFixture[str]) -> None:
    scheduled = """\
name: nightly

on:
  schedule:
    - cron: "0 4 * * *"

permissions: {}

jobs:
  nightly:
    runs-on: ubuntu-latest
    steps:
      - run: echo nightly
"""
    files = InMemoryFiles()
    files.write_text(".github/workflows/nightly.yml", scheduled)

    assert workflows_check.run(_args(owner=None), files=files) == ExitCode.OK

    with pytest.raises(ValidationError, match="1 workflow invariant violation"):
        workflows_check.run(_args(owner="ocx-sh"), files=files)
    assert "[WF-07]" in capsys.readouterr().err


def test_a_file_that_vanishes_between_listing_and_reading_is_skipped() -> None:
    """`list_files` then `read_text` is two calls; a file removed in between
    reads back as `None` and must not become an empty workflow with no
    `permissions:` block — a WF-01 finding invented out of a race."""

    class VanishingFiles(InMemoryFiles):
        def read_text(self, path: str) -> str | None:
            return None if path.endswith("gone.yml") else super().read_text(path)

    files = VanishingFiles()
    files.write_text(".github/workflows/ci.yml", _CLEAN)
    files.write_text(".github/workflows/gone.yml", _CLEAN)

    assert workflows_check.run(_args(), files=files) == ExitCode.OK
