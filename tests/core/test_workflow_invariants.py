# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""One planted violation per workflow invariant, plus the near-misses.

Every rule here was watched red against the fixture below it and green against
`ocx-sh/index`'s real ten-workflow tree — which is where three of them were
found to be too strict on their first draft: WF-04 flagged a
`github.event_name != 'schedule'` cron guard, WF-06 flagged a gate job holding
`pull-requests: write`, and WF-07 flagged jobs guarded transitively through
`needs:`. The `_allows_`/`_ignores_` tests below pin those three near-misses,
because a check that only ever ran against a broken subject is not proven.

Fixtures are inline strings rather than files: a workflow small enough to read
in one screen makes the planted violation obvious, and DAMP beats a fixture
directory you have to go open (`docs/reference/contracts.md` §2).
"""

from __future__ import annotations

from ocx_indexbot.ci import render
from ocx_indexbot.core.workflow_invariants import (
    check_workflows,
    job_block,
    job_names,
    job_steps,
    triggers,
    uses_refs,
)
from tests.fakes import make_policy

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


_WORKFLOW_RUN = f"""\
name: label

on:
  workflow_run:
    workflows: ["validate"]
    types: [completed]

permissions: {{}}

jobs:
  label:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@{_SHA}
        with:
          persist-credentials: false
      - name: Label
        run: indexbot label-failed-run --head-sha "$HEAD_SHA"
"""


def test_wf05_flags_a_ref_key_under_workflow_run() -> None:
    """`workflow_run` carries base-repo privileges exactly like
    `pull_request_target`, and the run it reports on is the one a fork PR just
    caused — so `github.event.workflow_run.head_sha` names fork-authored code,
    and a `ref:` resolving it executes that code with the base repository's
    credentials."""
    with_ref = _WORKFLOW_RUN.replace(
        "          persist-credentials: false\n",
        "          persist-credentials: false\n"
        "          ref: ${{ github.event.workflow_run.head_sha }}\n",
    )
    assert _rules(with_ref) == ["WF-05"]


def test_wf05_flags_a_ref_key_under_issue_comment() -> None:
    """The third trigger in the same class, and the textbook one: anybody who
    can comment fires it, and it runs with the base repository's token. A
    `/retest` handler that checks out the commented-on PR's head is the
    canonical `pull_request_target` breach reached through a comment box."""
    handler = f"""\
name: retest

on:
  issue_comment:
    types: [created]

permissions: {{}}

jobs:
  retest:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@{_SHA}
        with:
          persist-credentials: false
          ref: refs/pull/${{{{ github.event.issue.number }}}}/head
"""
    assert _rules(handler) == ["WF-05"]


def test_wf05_allows_the_default_ref_under_workflow_run() -> None:
    """What `pr-checks-label.yml` renders: a checkout of the default branch,
    because running the bot needs the repository at all. The failed run's head
    sha reaches the command as an argument, never as a ref."""
    assert _rules(_WORKFLOW_RUN) == []


# --- WF-06: a contents:write job must not persist checkout credentials -------
#
# This rule used to say a `contents: write` job under `pull_request_target`
# could run no `uses:` step at all. It was retired when arming auto-merge
# became `indexbot governance-gate --arm-only`, which needs a checkout and a
# setup step — see the check's own docstring for what that traded away and
# what replaced it. The tests below pin the narrowed rule, and the two
# `_allows_` cases pin that the narrowing is real rather than accidental.


def test_wf06_flags_a_contents_write_job_that_persists_checkout_credentials() -> None:
    """The default `persist-credentials: true` writes the job's token into
    `.git/config`, where every later step inherits it through plain `git` —
    dependency resolution and build backends included — with no
    `GITHUB_TOKEN` in sight to audit."""
    writing = _PRIVILEGED.replace("      pull-requests: write", "      contents: write")
    assert _rules(writing) == ["WF-06"]


def test_wf06_allows_a_contents_write_job_that_opts_out() -> None:
    """What `governance.yml`'s `arm-auto-merge` job actually renders."""
    guarded = _PRIVILEGED.replace("      pull-requests: write", "      contents: write").replace(
        f"      - uses: actions/checkout@{_SHA}\n",
        f"      - uses: actions/checkout@{_SHA}\n"
        "        with:\n"
        "          persist-credentials: false\n",
    )
    assert _rules(guarded) == []


def test_wf06_flags_a_second_checkout_the_first_ones_opt_out_does_not_cover() -> None:
    """The opt-out is a property of one step's `with:`, not of the job. A
    second checkout under `path:` takes the default `persist-credentials:
    true` and writes the same `contents: write` token into
    `second/.git/config` — while a job-scoped search reads the first step's
    hardening as vouching for it."""
    two = _PRIVILEGED.replace("      pull-requests: write", "      contents: write").replace(
        f"      - uses: actions/checkout@{_SHA}\n",
        f"      - uses: actions/checkout@{_SHA}\n"
        "        with:\n"
        "          persist-credentials: false\n"
        f"      - uses: actions/checkout@{_SHA}\n"
        "        with:\n"
        "          path: second\n",
    )
    assert _rules(two) == ["WF-06"]


def test_wf06_flags_a_contents_write_job_that_hands_an_action_the_token() -> None:
    """The hazard the retired blanket `uses:` ban actually named, and the one
    half `persist-credentials: false` cannot reach: the credential never goes
    near `.git/config`, it goes straight into an action's inputs. No checkout
    is needed to do it."""
    forwarding = _PRIVILEGED.replace("      pull-requests: write", "      contents: write").replace(
        f"      - uses: actions/checkout@{_SHA}\n",
        "      - uses: ./.github/actions/setup-bot\n"
        "        with:\n"
        "          token: ${{ github.token }}\n",
    )
    assert _rules(forwarding) == ["WF-06"]


def test_wf06_flags_a_secret_handed_to_an_action() -> None:
    """`secrets.*` is the same hand-off wearing a different name — and unlike
    `github.token` it is not even scoped by the job's `permissions:` block."""
    forwarding = _PRIVILEGED.replace("      pull-requests: write", "      contents: write").replace(
        f"      - uses: actions/checkout@{_SHA}\n",
        f"      - uses: acme/publish@{_SHA}\n"
        "        env:\n"
        "          KEY: ${{ secrets.SIGNING_KEY }}\n",
    )
    assert _rules(forwarding) == ["WF-06"]


def test_wf06_allows_the_shape_the_generator_renders() -> None:
    """Checkout hardened, setup action handed nothing, and the token named in
    the `run:` step's own `env:` — the one place a `contents: write` job is
    supposed to spend it, where a reviewer reading the file can see it."""
    rendered = (
        _PRIVILEGED.replace("      pull-requests: write", "      contents: write")
        .replace(
            f"      - uses: actions/checkout@{_SHA}\n",
            f"      - uses: actions/checkout@{_SHA}\n"
            "        with:\n"
            "          persist-credentials: false\n"
            "      - uses: ./.github/actions/setup-bot\n",
        )
        .replace(
            "      - name: Gate\n",
            "      - name: Gate\n        env:\n          GITHUB_TOKEN: ${{ github.token }}\n",
        )
    )
    assert _rules(rendered) == []


def test_wf06_sees_a_job_whose_header_carries_a_trailing_comment() -> None:
    """`  arm-auto-merge:  # arms the merge` is a job. A header pattern
    anchored straight on end-of-line reads it as ordinary content, and every
    job-scoped rule — WF-06 here, WF-07 below — then skips the job outright."""
    commented = _PRIVILEGED.replace("      pull-requests: write", "      contents: write").replace(
        "  gate:\n", "  gate:  # the governance gate\n"
    )
    assert _rules(commented) == ["WF-06"]


def test_wf06_reaches_workflow_run_too() -> None:
    """Same argument as WF-05: `workflow_run` is a privileged trigger a fork
    PR can cause to fire."""
    writing = _WORKFLOW_RUN.replace("        with:\n          persist-credentials: false\n", "")
    assert _rules(writing) == ["WF-06"]


def test_wf06_reads_steps_written_at_the_key_column() -> None:
    """YAML lets a sequence sit at its key's own indentation, GitHub accepts
    it and `actionlint` says nothing. A step scan keyed on a fixed bullet
    column finds no steps at all in such a job — and "no steps" reads as
    "nothing to flag", which is the worst possible way to be wrong here."""
    compact = f"""\
name: governance

on:
  pull_request_target:

permissions: {{}}

jobs:
  gate:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
    - uses: actions/checkout@{_SHA}
    - name: Gate
      run: indexbot governance-gate
"""
    assert _rules(compact) == ["WF-06"]


def test_wf06_ignores_other_write_scopes() -> None:
    """The near-miss `governance.yml` exposed: a gate job legitimately holds
    `pull-requests`/`statuses`/`issues: write` while running this bot, and
    none of those can move the base branch."""
    assert _rules(_PRIVILEGED) == []


def test_wf06_ignores_a_contents_write_job_that_checks_nothing_out() -> None:
    """No checkout, no `.git/config` to leak into. The old shape of
    `arm-auto-merge`, which stays valid for a deployment whose `ci.run` needs
    no repository."""
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


def test_wf06_recognises_a_forked_checkout_action() -> None:
    """Matched on the action's path, not its owner: the credential-persisting
    behaviour travels with the action, so a vendored re-publish is the same
    hazard under a different name."""
    writing = _PRIVILEGED.replace("      pull-requests: write", "      contents: write").replace(
        "actions/checkout@", "acme/vendored/checkout@"
    )
    assert _rules(writing) == ["WF-06"]


def test_wf06_does_not_reach_the_unprivileged_trigger() -> None:
    """Scoped to `pull_request_target`, the trigger where a job holds real
    credentials while a pull request it did not author decides when it runs."""
    unprivileged = _PRIVILEGED.replace("  pull_request_target:", "  pull_request:").replace(
        "      pull-requests: write", "      contents: write"
    )
    assert _rules(unprivileged) == []


# --- WF-08: a contents:write job must not resolve the bot at job start -------
#
# The hole no other rule here could see. WF-02 inspects `uses:` refs; this is
# about how a `run:` command resolves a *package*, and the two never met. Every
# case below was watched red or green against the same `_PRIVILEGED` fixture
# WF-06 uses, since they ask different questions about one job shape.

_ARMING = _PRIVILEGED.replace("      pull-requests: write", "      contents: write").replace(
    f"      - uses: actions/checkout@{_SHA}\n",
    f"      - uses: actions/checkout@{_SHA}\n        with:\n          persist-credentials: false\n",
)
"""`arm-auto-merge` as `governance.yml` actually renders it — WF-06 already
satisfied, so a finding below is WF-08's and nothing else's."""


def test_wf08_flags_a_contents_write_job_that_resolves_the_bot_at_job_start() -> None:
    """The rendered shape of the documented default. `uvx ocx-indexbot` fetches
    the latest release when the step starts — no version, no lockfile, no hash
    — in the one job holding a token that can move an unprotected base branch
    and squash-merge a pull request."""
    floating = _ARMING.replace(
        "        run: indexbot governance-check", "        run: uvx ocx-indexbot governance-check"
    )
    assert _rules(floating) == ["WF-08"]


def test_wf08_flags_a_lockfile_invocation_that_may_re_resolve() -> None:
    """`uv run --project bot-tools -- indexbot` reads like a lockfile pin and
    is not one: `uv run` re-locks when the lockfile is stale against
    `pyproject.toml`, and re-locking a git source moves the commit."""
    unfrozen = _ARMING.replace(
        "        run: indexbot governance-check",
        "        run: uv run --project bot-tools -- indexbot governance-check",
    )
    assert _rules(unfrozen) == ["WF-08"]


def test_wf08_allows_a_frozen_lockfile_invocation() -> None:
    """What `governance.yml` renders for a deployment that pins by lockfile —
    `ocx-sh/index`'s own shape, once `--frozen` makes the lock binding."""
    frozen = _ARMING.replace(
        "        run: indexbot governance-check",
        "        run: uv run --project bot-tools --frozen -- indexbot governance-check",
    )
    assert _rules(frozen) == []


def test_wf08_allows_an_invocation_that_names_no_resolver() -> None:
    """`_ARMING` itself: a bare `indexbot`, resolved by whatever the image or
    the setup step put on `$PATH`. This rule refuses runtime resolution it can
    see; it does not dictate a toolchain, or "any index, any forge" would mean
    "any index that uses uv"."""
    assert _rules(_ARMING) == []


def test_wf08_reads_a_comment_as_prose_not_as_a_step() -> None:
    """These files argue for their own rules at length beside them, and
    `governance.yml`'s arm job discusses a `uv` resolution failure in prose.
    A finding raised off a comment would be a rule nobody could keep green."""
    commented = _ARMING.replace(
        "      - name: Gate\n",
        "      # uvx ocx-indexbot is what this must never become\n      - name: Gate\n",
    )
    assert _rules(commented) == []


def test_wf08_ignores_a_job_that_cannot_move_the_base_branch() -> None:
    """Scoped to `contents: write`, like WF-06. The unprivileged validate lane
    resolves the bot at job start too, and that is fine — it holds no token, so
    a compromised release there reaches nothing a fork's runner did not already
    have."""
    reading = _PRIVILEGED.replace(
        "        run: indexbot governance-check", "        run: uvx ocx-indexbot governance-check"
    )
    assert _rules(reading) == []


def test_wf08_does_not_reach_the_unprivileged_trigger() -> None:
    unprivileged = _ARMING.replace("  pull_request_target:", "  pull_request:").replace(
        "        run: indexbot governance-check", "        run: uvx ocx-indexbot governance-check"
    )
    assert _rules(unprivileged) == []


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


def test_wf07_flags_a_guard_that_only_skips_one_step() -> None:
    """A step-level `if:` skips a step. The job still runs on the fork, with
    its token and every other step in it."""
    stepwise = _SCHEDULED.replace("    if: github.repository_owner == 'ocx-sh'\n", "").replace(
        "      - name: Guard\n",
        "      - name: Guard\n        if: github.repository_owner == 'ocx-sh'\n",
    )
    assert _rules(stepwise) == ["WF-07", "WF-07", "WF-07"]


def test_wf07_flags_a_guard_left_behind_as_a_comment() -> None:
    """The most plausible way this rule ever goes quiet: the guard is removed
    and its expression kept as a note about what used to be there."""
    commented = _SCHEDULED.replace(
        "    if: github.repository_owner == 'ocx-sh'",
        "    # guard used to be: github.repository_owner == 'ocx-sh'",
    )
    assert _rules(commented) == ["WF-07", "WF-07", "WF-07"]


def test_wf07_flags_a_guard_demoted_to_a_trailing_comment() -> None:
    demoted = _SCHEDULED.replace(
        "    if: github.repository_owner == 'ocx-sh'",
        "    if: ${{ !cancelled() }}  # github.repository_owner == 'ocx-sh'",
    )
    assert _rules(demoted) == ["WF-07", "WF-07", "WF-07"]


def test_wf07_allows_a_real_guard_carrying_a_trailing_comment() -> None:
    annotated = _SCHEDULED.replace(
        "    if: github.repository_owner == 'ocx-sh'",
        "    if: github.repository_owner == 'ocx-sh'  # every fork inherits this cron",
    )
    assert _rules(annotated) == []


def test_wf07_sees_a_job_whose_header_carries_a_trailing_comment() -> None:
    """The guarded job is the entry point the other two inherit from. Lose its
    header to a trailing comment and both dependents look unguarded."""
    commented = _SCHEDULED.replace("  guard:\n", "  guard:  # the one entry point\n")
    assert _rules(commented) == []


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


def test_job_names_reads_a_header_carrying_a_trailing_comment() -> None:
    assert job_names("jobs:\n  a:  # first\n  b:\n") == ["a", "b"]


def test_job_block_finds_a_header_carrying_a_trailing_comment() -> None:
    text = "jobs:\n  a:  # first\n    runs-on: x\n  b:\n    runs-on: y\n"
    assert job_block(text, "a") == "  a:  # first\n    runs-on: x"


def test_job_block_is_not_ended_by_a_comment_at_the_header_column() -> None:
    """A comment indented to the job-header column is not a job header. Ending
    the block there would hide every step below it from WF-06."""
    text = "jobs:\n  a:\n    runs-on: x\n  # a note\n    steps:\n      - run: echo hi\n"
    assert "echo hi" in job_block(text, "a")


def test_job_steps_splits_on_the_step_bullet() -> None:
    """A step owns everything indented under its own bullet — including a
    nested list, which is why the split is keyed on the bullet column rather
    than on any `- ` at all."""
    block = """\
  a:
    steps:
      # a note before the first step
      - uses: acme/one
        with:
          paths:
            - src
            - tests

      - run: echo two
"""
    one, two = job_steps(block)
    assert "acme/one" in one and "- tests" in one and "echo two" not in one
    assert two.strip() == "- run: echo two"


def test_job_steps_of_a_job_without_steps_is_empty() -> None:
    assert job_steps("  a:\n    runs-on: x\n") == []


def test_job_steps_reads_bullets_at_the_key_column() -> None:
    """`steps:` and its items at the same indentation is valid YAML that
    GitHub runs — the bullet column is taken from the first item, not
    assumed."""
    block = "  a:\n    steps:\n    - uses: acme/one\n    - run: echo two\n"
    assert [step.strip() for step in job_steps(block)] == ["- uses: acme/one", "- run: echo two"]


def test_job_steps_of_a_steps_key_with_no_items_is_empty() -> None:
    assert job_steps("  a:\n    steps:") == []


# --- the tree this package itself generates ----------------------------------


def test_the_generated_workflow_tree_satisfies_every_invariant() -> None:
    """Generator and auditor ship together, so a rule the rendered tree cannot
    satisfy is a bug in one of them. Caught here rather than in the deployment
    that re-renders and then fails its own `workflows-check`."""
    plan = render.build_render_plan(make_policy(deploy_workflow="render-deploy.yml"), existing={})
    workflows = {path.rsplit("/", 1)[-1]: text for path, text in plan.items()}
    assert check_workflows(workflows, owner="ocx-sh") == ()
