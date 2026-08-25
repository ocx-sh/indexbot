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


_ARMING_VIA_SETUP = """\
name: governance

on:
  pull_request_target:

permissions: {}

jobs:
  arm-auto-merge:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: ./.github/actions/setup-bot
      - name: Arm
        run: uv run --frozen --project bot-tools -- indexbot governance-gate --arm-only
"""
"""The credentialed job as `indexbot ci` renders it when the deployment
declares a `ci.setup`: pinned in the workflow, and everything that decides
what actually runs one file away."""

_UNPINNED_ACTION = """\
name: setup-bot
description: install the bot
runs:
  using: composite
  steps:
    - shell: bash
      run: uvx ocx-indexbot --version
"""


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


def test_a_local_composite_action_beside_the_workflow_directory_is_loaded() -> None:
    """`ci.setup` renders a `uses:` step into the credentialed job, and a
    composite action's `run:` steps execute there with that job's token. The
    action lives in `.github/actions/`, a SIBLING of the workflow directory —
    so finding it means walking out of `--dir` by one level, which is the only
    reason this loader is not part of `_load_workflows`."""
    files = InMemoryFiles()
    files.write_text(".github/workflows/governance.yml", _ARMING_VIA_SETUP)
    files.write_text(".github/actions/setup-bot/action.yml", _UNPINNED_ACTION)

    with pytest.raises(ValidationError, match="1 workflow invariant violation"):
        workflows_check.run(_args(), files=files)


def test_a_composite_action_that_pins_what_it_installs_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = InMemoryFiles()
    files.write_text(".github/workflows/governance.yml", _ARMING_VIA_SETUP)
    files.write_text(
        ".github/actions/setup-bot/action.yml",
        _UNPINNED_ACTION.replace("uvx ocx-indexbot --version", "uv sync --frozen"),
    )

    assert workflows_check.run(_args(), files=files) == ExitCode.OK
    assert "1 workflow(s) audited, no findings" in capsys.readouterr().err


def test_a_workflow_directory_with_no_parent_loads_no_actions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--dir workflows` has nowhere to look for a sibling `actions/`. That is
    a caller getting less coverage, never a crash."""
    files = InMemoryFiles()
    files.write_text("workflows/governance.yml", _ARMING_VIA_SETUP)
    files.write_text("actions/setup-bot/action.yml", _UNPINNED_ACTION)

    assert workflows_check.run(_args(directory="workflows"), files=files) == ExitCode.OK
    assert "1 workflow(s) audited, no findings" in capsys.readouterr().err


def test_a_non_action_file_under_the_actions_tree_is_ignored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only `action.yml`/`action.yaml` is a composite action. A README or a
    fixture beside it is not one, and reading it as one would let its prose
    decide a security verdict."""
    files = InMemoryFiles()
    files.write_text(".github/workflows/governance.yml", _ARMING_VIA_SETUP)
    files.write_text(
        ".github/actions/setup-bot/action.yml",
        _UNPINNED_ACTION.replace("uvx ocx-indexbot --version", "uv sync --frozen"),
    )
    files.write_text(".github/actions/setup-bot/notes.yml", "run: uvx ocx-indexbot")

    assert workflows_check.run(_args(), files=files) == ExitCode.OK
    assert "1 workflow(s) audited, no findings" in capsys.readouterr().err


_GITLAB_INCLUDED_FLOATING_IMAGE = "build:\n  image: python:3.13\n  script:\n    - echo hi\n"


def test_gitlab_include_local_target_outside_dir_is_loaded_and_audited() -> None:
    """The reported hole: a `local:` include naming a file that lives outside
    `--dir` entirely used to be invisible — the audit read the root file plus
    whatever `--dir` walked and nothing `include:` actually pointed at."""
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", "include:\n  - local: ci/jobs.yml\n\n" + _GITLAB_CLEAN)
    files.write_text("ci/jobs.yml", _GITLAB_INCLUDED_FLOATING_IMAGE)

    with pytest.raises(ValidationError, match="1 pipeline invariant violation"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_bare_scalar_shorthand_is_treated_as_local() -> None:
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", "include: ci/jobs.yml\n\n" + _GITLAB_CLEAN)
    files.write_text("ci/jobs.yml", _GITLAB_INCLUDED_FLOATING_IMAGE)

    with pytest.raises(ValidationError, match="1 pipeline invariant violation"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_local_leading_slash_is_project_root_relative() -> None:
    """GitLab documents `templates/x.yml` and `/templates/x.yml` as
    identical, both relative to the project root."""
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", "include:\n  - local: /ci/jobs.yml\n\n" + _GITLAB_CLEAN)
    files.write_text("ci/jobs.yml", _GITLAB_INCLUDED_FLOATING_IMAGE)

    with pytest.raises(ValidationError, match="1 pipeline invariant violation"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_local_target_that_does_not_exist_raises() -> None:
    """A clean audit over a pipeline this loader never fully read is the bug
    — an unresolvable `local:` target must fail loudly, not be skipped."""
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", "include:\n  - local: ci/missing.yml\n\n" + _GITLAB_CLEAN)

    with pytest.raises(ValidationError, match="does not resolve"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_local_path_traversal_raises() -> None:
    """`..` in a `local:` value goes through `FilePort.read_text` unchanged —
    the same untrusted-path discipline every other read in this package
    uses, not a bespoke check in the loader."""
    files = InMemoryFiles()
    files.write_text(
        ".gitlab-ci.yml", "include:\n  - local: '../../etc/passwd'\n\n" + _GITLAB_CLEAN
    )

    with pytest.raises(ValidationError, match="path escapes root"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_remote_raises() -> None:
    files = InMemoryFiles()
    files.write_text(
        ".gitlab-ci.yml", "include:\n  - remote: 'https://example.com/ci.yml'\n\n" + _GITLAB_CLEAN
    )

    with pytest.raises(ValidationError, match="remote:"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_project_raises() -> None:
    files = InMemoryFiles()
    files.write_text(
        ".gitlab-ci.yml",
        "include:\n  - project: 'group/other'\n    file: '/ci.yml'\n\n" + _GITLAB_CLEAN,
    )

    with pytest.raises(ValidationError, match="project:"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_template_raises() -> None:
    files = InMemoryFiles()
    files.write_text(
        ".gitlab-ci.yml",
        "include:\n  - template: Auto-DevOps.gitlab-ci.yml\n\n" + _GITLAB_CLEAN,
    )

    with pytest.raises(ValidationError, match="template:"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_entry_with_a_conditional_rules_key_is_still_local() -> None:
    """A `local:` include entry may carry its own `rules:` (GitLab's
    per-include condition) — a line that is none of the four recognised
    forms is skipped rather than mistaken for one of them."""
    files = InMemoryFiles()
    files.write_text(
        ".gitlab-ci.yml",
        "include:\n"
        "  - local: ci/jobs.yml\n"
        "    rules:\n"
        "      - if: '$CI_PIPELINE_SOURCE'\n\n" + _GITLAB_CLEAN,
    )
    files.write_text("ci/jobs.yml", _GITLAB_INCLUDED_FLOATING_IMAGE)

    with pytest.raises(ValidationError, match="1 pipeline invariant violation"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_follows_a_nested_include_in_an_included_file() -> None:
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", "include:\n  - local: ci/jobs.yml\n\n" + _GITLAB_CLEAN)
    files.write_text(
        "ci/jobs.yml", "include:\n  - local: ci/deploy.yml\n\nbuild:\n  script:\n    - echo hi\n"
    )
    files.write_text("ci/deploy.yml", _GITLAB_INCLUDED_FLOATING_IMAGE)

    with pytest.raises(ValidationError, match="1 pipeline invariant violation"):
        workflows_check.run(_args(None, forge="gitlab"), files=files)


def test_gitlab_include_cycle_does_not_loop_forever() -> None:
    """Two files including each other is invalid GitLab in practice (real
    GitLab caps include depth), but this loader must still terminate rather
    than rediscover the same target forever — `loaded` membership is checked
    before a target is queued for a second pass."""
    files = InMemoryFiles()
    files.write_text(".gitlab-ci.yml", "include:\n  - local: ci/a.yml\n\n" + _GITLAB_CLEAN)
    files.write_text("ci/a.yml", "include:\n  - local: ci/b.yml\n")
    files.write_text("ci/b.yml", "include:\n  - local: ci/a.yml\n")

    assert workflows_check.run(_args(None, forge="gitlab"), files=files) == ExitCode.OK


def test_a_composite_action_that_vanishes_between_listing_and_reading_is_skipped() -> None:
    """Same race as the workflow loader's, same answer. An action that reads
    back as `None` must not become an empty one whose absent `run:` steps
    silently satisfy WF-08 — the workflow it was called from is still audited
    on what it says itself."""

    class VanishingFiles(InMemoryFiles):
        def read_text(self, path: str) -> str | None:
            return None if path.endswith("action.yml") else super().read_text(path)

    files = VanishingFiles()
    files.write_text(".github/workflows/governance.yml", _ARMING_VIA_SETUP)
    files.write_text(".github/actions/setup-bot/action.yml", _UNPINNED_ACTION)

    assert workflows_check.run(_args(), files=files) == ExitCode.OK
