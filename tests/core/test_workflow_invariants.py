# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""One planted violation per workflow invariant, plus the near-misses.

Every rule here was watched red against the fixture below it and green against
`ocx-sh/index`'s real ten-workflow tree — which is where three of them were
found to be too strict on their first draft: WF-04 flagged a
`github.event_name != 'schedule'` cron guard, WF-06 flagged a gate job holding
`pull-requests: write`, and WF-07 flagged jobs guarded transitively through
`needs:`. The `_allows_*` tests below pin those three near-misses, because a
check that only ever ran against a broken subject is not proven.

Fixtures are inline strings rather than files: a workflow small enough to read
in one screen makes the planted violation obvious, and DAMP beats a fixture
directory you have to go open (`docs/reference/contracts.md` §2).
"""

from __future__ import annotations

from ocx_indexbot.core.workflow_invariants import (
    check_workflows,
    job_block,
    job_names,
    triggers,
    uses_refs,
)

_SHA = "a" * 40

_CLEAN = f"""\
name: ci

on:
  pull_request:

permissions: {{}}

jobs:
  verify:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@{_SHA}  # v7.0.1
      - uses: ./.github/actions/setup
      - uses: docker://alpine:3.20
      - name: Verify
        run: task verify
"""


def _rules(text: str, *, owner: str | None = "ocx-sh", name: str = "wf.yml") -> list[str]:
    return [finding.rule for finding in check_workflows({name: text}, owner=owner)]


def test_a_clean_workflow_has_no_findings() -> None:
    assert check_workflows({"ci.yml": _CLEAN}, owner="ocx-sh") == ()


def test_findings_are_sorted_by_workflow_name() -> None:
    broken = _CLEAN.replace("permissions: {}\n", "")
    findings = check_workflows({"z.yml": broken, "a.yml": broken}, owner="ocx-sh")
    assert [finding.workflow for finding in findings] == ["a.yml", "z.yml"]


def test_finding_str_names_the_file_and_the_rule() -> None:
    (finding,) = check_workflows({"ci.yml": _CLEAN.replace("permissions: {}\n", "")}, owner=None)
    assert str(finding).startswith("ci.yml: [WF-01] ")


# --- WF-01: top-level default-deny -----------------------------------------


def test_wf01_flags_a_workflow_without_top_level_default_deny() -> None:
    assert _rules(_CLEAN.replace("permissions: {}\n", "")) == ["WF-01"]


def test_wf01_allows_a_commented_default_deny() -> None:
    assert _rules(_CLEAN.replace("permissions: {}", "permissions: {}  # jobs elevate")) == []


# --- WF-02: SHA-pinned actions ----------------------------------------------


def test_wf02_flags_a_floating_action_ref() -> None:
    assert _rules(_CLEAN.replace(f"actions/checkout@{_SHA}", "actions/checkout@v4")) == ["WF-02"]


def test_wf02_flags_a_short_sha() -> None:
    assert _rules(_CLEAN.replace(f"actions/checkout@{_SHA}", "actions/checkout@abc1234")) == [
        "WF-02"
    ]


def test_wf02_allows_local_and_docker_refs() -> None:
    """Both appear in `_CLEAN`; neither is a marketplace ref a tag could move."""
    assert uses_refs(_CLEAN) == [
        f"actions/checkout@{_SHA}",
        "./.github/actions/setup",
        "docker://alpine:3.20",
    ]
    assert _rules(_CLEAN) == []


# --- WF-03 / WF-04: the trigger split ---------------------------------------

_BOTH_TRIGGERS = f"""\
name: gate

on:
  pull_request:
  pull_request_target:

permissions: {{}}

jobs:
  gate:
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'
    steps:
      - uses: actions/checkout@{_SHA}
"""


def test_wf03_and_wf04_flag_a_workflow_carrying_both_pr_triggers() -> None:
    assert _rules(_BOTH_TRIGGERS) == ["WF-03", "WF-04"]


def test_wf04_flags_a_pr_event_name_guard_even_with_one_trigger() -> None:
    single = _BOTH_TRIGGERS.replace("  pull_request_target:\n", "")
    assert _rules(single) == ["WF-04"]


def test_wf04_allows_a_schedule_exclusion_guard() -> None:
    """The near-miss that `ocx-sh/index`'s own `ci.yml` exposed: excluding the
    nightly run from every PR gate is unrelated to the check-run collision."""
    scheduled = f"""\
name: ci

on:
  pull_request:
  schedule:
    - cron: "0 4 * * *"

permissions: {{}}

jobs:
  verify:
    runs-on: ubuntu-latest
    if: github.event_name != 'schedule'
    steps:
      - uses: actions/checkout@{_SHA}
"""
    assert _rules(scheduled) == []


# --- WF-05: no PR-head checkout in a credentialed workflow -------------------

_PRIVILEGED = f"""\
name: governance

on:
  pull_request_target:

permissions: {{}}

jobs:
  gate:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@{_SHA}
      - name: Gate
        run: indexbot governance-check --pr-number "$PR_NUMBER"
"""


def test_wf05_flags_a_ref_key_under_pull_request_target() -> None:
    with_ref = _PRIVILEGED.replace(
        f"      - uses: actions/checkout@{_SHA}\n",
        f"      - uses: actions/checkout@{_SHA}\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n",
    )
    assert _rules(with_ref) == ["WF-05"]


def test_wf05_allows_a_ref_key_under_the_unprivileged_trigger() -> None:
    unprivileged = _PRIVILEGED.replace("  pull_request_target:", "  pull_request:").replace(
        f"      - uses: actions/checkout@{_SHA}\n",
        f"      - uses: actions/checkout@{_SHA}\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n",
    )
    assert _rules(unprivileged) == []


# --- WF-06: a contents:write job runs nothing --------------------------------


def test_wf06_flags_a_contents_write_job_that_runs_repository_code() -> None:
    writing = _PRIVILEGED.replace("      pull-requests: write", "      contents: write")
    assert _rules(writing) == ["WF-06"]


def test_wf06_allows_other_write_scopes_beside_uses_steps() -> None:
    """The near-miss `governance.yml` exposed: a gate job legitimately holds
    `pull-requests`/`statuses`/`issues: write` while running this bot."""
    assert _rules(_PRIVILEGED) == []


def test_wf06_allows_contents_write_in_a_job_that_runs_no_code() -> None:
    arming = """\
name: governance

on:
  pull_request_target:

permissions: {}

jobs:
  arm:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: Arm auto-merge
        run: gh pr merge --auto --squash "$PR_NUMBER"
"""
    assert _rules(arming) == []


# --- WF-07: cron is upstream-only --------------------------------------------

_SCHEDULED = """\
name: nightly

on:
  schedule:
    - cron: "0 4 * * *"

permissions: {}

jobs:
  guard:
    runs-on: ubuntu-latest
    if: github.repository_owner == 'ocx-sh'
    steps:
      - name: Guard
        run: echo upstream
  deploy:
    runs-on: ubuntu-latest
    needs: guard
    steps:
      - name: Deploy
        run: echo deploying
  smoke:
    runs-on: ubuntu-latest
    needs: [deploy]
    steps:
      - name: Smoke
        run: echo smoking
"""


def test_wf07_allows_a_guard_inherited_transitively_through_needs() -> None:
    """`deploy` needs the guarded `guard`; `smoke` needs `deploy`. A skipped
    dependency skips its dependents, so neither runs on a fork."""
    assert _rules(_SCHEDULED) == []


def test_wf07_flags_every_unguarded_job_reachable_by_a_schedule() -> None:
    unguarded = _SCHEDULED.replace("    if: github.repository_owner == 'ocx-sh'\n", "")
    assert _rules(unguarded) == ["WF-07", "WF-07", "WF-07"]


def test_wf07_flags_a_job_needing_an_unguarded_job() -> None:
    detached = _SCHEDULED.replace("    needs: guard\n", "")
    assert [
        f.message.split("`")[1] for f in check_workflows({"n.yml": detached}, owner="ocx-sh")
    ] == [
        "deploy",
        "smoke",
    ]


def test_wf07_allows_a_job_excluding_the_schedule_event() -> None:
    excluded = _SCHEDULED.replace(
        "    if: github.repository_owner == 'ocx-sh'", "    if: github.event_name != 'schedule'"
    )
    assert _rules(excluded) == []


def test_wf07_needs_a_missing_job_is_not_a_guard() -> None:
    dangling = _SCHEDULED.replace("    needs: guard", "    needs: nonexistent")
    assert [
        f.message.split("`")[1] for f in check_workflows({"n.yml": dangling}, owner="ocx-sh")
    ] == [
        "deploy",
        "smoke",
    ]


def test_wf07_terminates_on_a_needs_cycle() -> None:
    """Invalid YAML GitHub itself rejects — but the walk must terminate rather
    than trust the input."""
    cyclic = """\
name: nightly

on:
  schedule:
    - cron: "0 4 * * *"

permissions: {}

jobs:
  a:
    runs-on: ubuntu-latest
    needs: b
    steps:
      - run: echo a
  b:
    runs-on: ubuntu-latest
    needs: a
    steps:
      - run: echo b
"""
    assert _rules(cyclic) == ["WF-07", "WF-07"]


def test_wf07_is_skipped_without_an_owner() -> None:
    unguarded = _SCHEDULED.replace("    if: github.repository_owner == 'ocx-sh'\n", "")
    assert _rules(unguarded, owner=None) == []


def test_wf07_ignores_a_workflow_with_no_schedule() -> None:
    assert _rules(_CLEAN) == []


# --- parser helpers ----------------------------------------------------------


def test_triggers_of_a_file_without_an_on_block_is_empty() -> None:
    assert triggers("name: broken\n") == frozenset()


def test_triggers_reads_an_on_block_that_runs_to_end_of_file() -> None:
    """No top-level key follows `on:` — the scan ends by exhausting the file,
    not by hitting the next column-zero key."""
    assert triggers("name: x\non:\n  push:\n  workflow_dispatch:\n") == frozenset(
        {"push", "workflow_dispatch"}
    )


def test_job_names_of_a_file_without_a_jobs_block_is_empty() -> None:
    assert job_names("name: broken\non:\n  push:\n") == []


def test_job_block_stops_at_the_next_job_header() -> None:
    block = job_block(_SCHEDULED, "deploy")
    assert block.startswith("  deploy:")
    assert "smoke" not in block


def test_job_block_of_the_last_job_runs_to_eof() -> None:
    assert job_block(_SCHEDULED, "smoke").rstrip().endswith("echo smoking")
