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


def _args(
    directory: str | None = ".github/workflows",
    owner: str | None = None,
    forge: str = "github",
) -> argparse.Namespace:
    return argparse.Namespace(dir=directory, owner=owner, forge=forge)


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


# --- --forge gitlab -----------------------------------------------------

_GITLAB_DIGEST = "0" * 64

_GITLAB_CLEAN = f"""\
default:
  image: python@sha256:{_GITLAB_DIGEST}

indexbot-validate:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - indexbot validate-pr
"""

_GITLAB_FLOATING_IMAGE = _GITLAB_CLEAN.replace(f"python@sha256:{_GITLAB_DIGEST}", "python:3.13")

_GITLAB_TOKEN_LEAK = _GITLAB_CLEAN.replace(
    "    - indexbot validate-pr",
    '    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com',
)

_GITLAB_JOB_TOKEN = _GITLAB_CLEAN.replace(
    "    - indexbot validate-pr",
    '    - curl -H "JOB-TOKEN: $CI_JOB_TOKEN" https://example.com',
)


def test_a_clean_gitlab_pipeline_exits_ok_and_reports_the_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_CLEAN)
    files.write_text(".gitlab-ci/indexbot.yml", _GITLAB_CLEAN)

    assert workflows_check.run(_args(None, forge="gitlab"), files=files) == ExitCode.OK

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "2 pipeline(s) audited, no findings" in captured.err


def test_gitlab_root_file_alone_is_audited_even_with_no_included_dir() -> None:
    """The root `.gitlab-ci.yml` is read directly, never discovered through
    `--dir` — a deployment with no `.gitlab-ci/` subdirectory at all still
    gets its one hand-written file audited."""
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_FLOATING_IMAGE)

    with pytest.raises(ValidationError, match="1 pipeline invariant violation"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_included_files_nest_at_any_depth() -> None:
    """Unlike GitHub's workflow directory, a GitLab `include: - local:` may
    name a file several directories deep, and it is still audited."""
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_CLEAN)
    files.write_text(".gitlab-ci/jobs/build.yml", _GITLAB_FLOATING_IMAGE)

    with pytest.raises(ValidationError, match="1 pipeline invariant violation"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_dir_flag_overrides_the_included_file_directory() -> None:
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_CLEAN)
    files.write_text("ci/build.yml", _GITLAB_FLOATING_IMAGE)

    with pytest.raises(ValidationError, match="1 pipeline invariant violation"):
        workflows_check.run(_args("ci", forge="gitlab"), files=files)


def test_gitlab_non_yaml_included_files_are_not_audited() -> None:
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_CLEAN)
    files.write_text(".gitlab-ci/README.md", "not a pipeline file")

    assert workflows_check.run(_args(None, forge="gitlab"), files=files) == ExitCode.OK


def test_a_gitlab_included_file_that_vanishes_between_listing_and_reading_is_skipped() -> None:
    """Same race as the github loader: `list_files` then `read_text` is two
    calls, and a file removed in between must not become an empty pipeline
    fragment invented out of thin air."""

    class VanishingFiles(InMemoryFiles):
        def read_text(self, path: str) -> str | None:
            return None if path.endswith("gone.yml") else super().read_text(path)

    files = VanishingFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_CLEAN)
    files.write_text(".gitlab-ci/gone.yml", _GITLAB_CLEAN)

    assert workflows_check.run(_args(None, forge="gitlab"), files=files) == ExitCode.OK


def test_gitlab_image_pin_violation_is_reported_as_gl01(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_FLOATING_IMAGE)

    with pytest.raises(ValidationError):
        workflows_check.run(_args(None, forge="gitlab"), files=files)
    assert "[GL-01]" in capsys.readouterr().err


def test_gitlab_token_on_merge_request_event_is_reported_as_gl03(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_TOKEN_LEAK)

    with pytest.raises(ValidationError):
        workflows_check.run(_args(None, forge="gitlab"), files=files)
    assert "[GL-03]" in capsys.readouterr().err


def test_gitlab_ci_job_token_is_exempt_from_gl03() -> None:
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_JOB_TOKEN)

    assert workflows_check.run(_args(None, forge="gitlab"), files=files) == ExitCode.OK


def test_an_empty_gitlab_pipeline_is_a_failure_not_a_pass() -> None:
    with pytest.raises(ValidationError, match="no pipeline files found"):
        workflows_check.run(_args(None, forge="gitlab"), files=InMemoryFiles())


def test_gitlab_owner_flag_is_a_no_op() -> None:
    """`--owner` only feeds WF-07, GitHub's cron guard. GitLab's schedule
    guard (GL-02) is `indexbot ci --check`'s job, not this one's — passing
    `--owner` here changes nothing."""
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", _GITLAB_CLEAN)

    assert (
        workflows_check.run(_args(None, owner="ocx-sh", forge="gitlab"), files=files) == ExitCode.OK
    )
