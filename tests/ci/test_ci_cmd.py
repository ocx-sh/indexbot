"""`indexbot ci` — the subcommand, through a real `FilePort`.

These tests assert on what actually lands on disk, because the value of the
generator is not that it substitutes strings correctly: it is that the file an
operator ends up committing carries the governance argument intact. So the
security-shaped properties — one trigger per workflow, the privileged job's
checkout, the cron guard — are asserted on rendered output, not on templates.
"""

from __future__ import annotations

import argparse

import pytest

from ocx_indexbot.ci import render
from ocx_indexbot.cli import ci_cmd
from ocx_indexbot.core.policy import IndexPolicy, RegistryConfig
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from tests.fakes import InMemoryFiles, make_policy

_VALIDATE = f"{render.GITHUB_DIR}/validate.yml"
_GOVERNANCE = f"{render.GITHUB_DIR}/governance.yml"


def _run(files: InMemoryFiles, *, check: bool = False, **policy: object) -> ExitCode:
    return ci_cmd.run(argparse.Namespace(check=check), files=files, policy=make_policy(**policy))


def _code(text: str) -> str:
    """`text` without its full-line comments.

    The generated files explain, at length, the hazards they exist to avoid —
    so `github.event_name` and `pull_request.head.sha` both appear in prose
    describing what must never be written. A test that grepped the raw file
    for those would fail on the explanation rather than on the code.
    """
    return "\n".join(line for line in text.split("\n") if not line.strip().startswith("#"))


def _rendered(**policy: object) -> InMemoryFiles:
    files = InMemoryFiles()
    assert _run(files, check=False, **policy) is ExitCode.OK
    return files


def test_add_arguments_registers_check() -> None:
    parser = argparse.ArgumentParser()
    ci_cmd.add_arguments(parser)
    assert parser.parse_args([]).check is False
    assert parser.parse_args(["--check"]).check is True


# --- what lands on disk ------------------------------------------------------


def test_writes_every_file_and_every_one_carries_the_header() -> None:
    files = _rendered()
    for name in (
        "validate.yml",
        "governance.yml",
        "reconcile.yml",
        "pr-checks-label.yml",
        "stale.yml",
    ):
        text = files.read_text(f"{render.GITHUB_DIR}/{name}")
        assert text is not None
        assert render.parse_header_version(text.split("\n")[0]) == render.TOOL_HEADER_VERSION


def test_the_privileged_and_unprivileged_halves_stay_in_separate_files() -> None:
    """The Block-tier rule. Both halves in one file means a job skipped by a
    `github.event_name` guard still emits a check run named after the required
    context, conclusion `skipped` — which GitHub counts as satisfied. A
    generator that merged them would emit a green-equivalent impostor of the
    gate it was generating."""
    files = _rendered()
    validate = _code(files.read_text(_VALIDATE) or "")
    governance = _code(files.read_text(_GOVERNANCE) or "")

    assert "\non:\n  pull_request:\n" in validate
    assert "pull_request_target" not in validate.split("permissions:")[0].split("\non:\n")[1]
    assert "pull_request_target:" in governance
    assert "\non:\n  pull_request:\n" not in governance
    assert "github.event_name" not in validate
    assert "github.event_name" not in governance


def test_the_privileged_workflow_never_checks_out_the_pull_request_head() -> None:
    """FP-7/G-16: `pull_request_target`'s default checkout is the base branch,
    and it must stay unset. A `ref:` at the PR head in a credentialed job is
    the single edit that breaks the whole safety argument.

    Asserted on the whole FILE, not just the gate job: `arm-auto-merge` now
    checks out too (it runs the bot), and it is the job holding
    `contents: write`, so it is the one where a `ref:` would be worst. `ref:`
    is the only way to check out PR head at all — the default is the base tip
    — which is why its absence is the property, rather than the absence of one
    particular expression."""
    governance = _code(_rendered().read_text(_GOVERNANCE) or "")
    assert "ref:" not in governance
    assert "pull_request.head.sha" not in governance.split("\n  arm-auto-merge:")[0]


# --- one job, one command ----------------------------------------------------


def _steps(text: str) -> list[str]:
    """Every `run:` line in a rendered workflow, comments already stripped."""
    return [
        line.split("run:", 1)[1].strip()
        for line in _code(text).split("\n")
        if line.strip().startswith("run:") or line.strip().startswith("- run:")
    ]


@pytest.mark.parametrize(
    ("workflow", "command"),
    [
        ("validate.yml", "validate-pr"),
        ("reconcile.yml", "reconcile --anomaly-ok"),
        ("pr-checks-label.yml", 'label-failed-run --head-sha "$HEAD_SHA"'),
        ("stale.yml", "stale"),
    ],
)
def test_each_single_job_workflow_runs_exactly_one_indexbot_command(
    workflow: str, command: str
) -> None:
    """The contract a hand-written pipeline is owed: read one job, copy one
    command. Every one of these workflows used to carry a shell program — a
    `git diff` pathspec, three exit-code translation steps, two `jq` filters —
    and every one of those was a security or observability control living in
    the least testable place available."""
    text = _rendered().read_text(f"{render.GITHUB_DIR}/{workflow}") or ""
    assert _steps(text) == [f"uv run --project bot-tools --frozen -- indexbot {command}"]


def test_the_governance_lane_is_two_jobs_and_two_commands() -> None:
    """The one deliberate exception to "one workflow, one job": the arm half
    holds `contents: write` and must run even when the gate FAILED, which one
    job cannot express. Both halves are still one command each."""
    text = _rendered().read_text(_GOVERNANCE) or ""
    assert _steps(text) == [
        'uv run --project bot-tools --frozen -- indexbot governance-gate --pr "$PR_NUMBER" '
        "--no-arm",
        'uv run --project bot-tools --frozen -- indexbot governance-gate --pr "$PR_NUMBER" '
        "--arm-only "
        '--disposition "$DISPOSITION" --head-sha "$HEAD_SHA"',
    ]


def test_the_arm_job_withdraws_even_when_the_gate_failed() -> None:
    """`if: ${{ !cancelled() }}`, never the `success()` a bare `needs:`
    implies. A gate that ERRORS — a forge 5xx, a malformed base-ref root, a
    `uv` resolution failure — would otherwise skip this job and leave an
    already-armed PR armed on an evaluation that never finished. A failed gate
    publishes an empty disposition, which `--arm-only` can only read as a
    withdraw. This `if:` is the entire reason the split survives; nothing else
    about it is load-bearing any more."""
    arm = _code(_rendered().read_text(_GOVERNANCE) or "").split("\n  arm-auto-merge:")[1]
    assert "if: ${{ !cancelled() }}" in arm


def test_only_the_arm_job_may_move_the_base_branch() -> None:
    """The permission scoping the two-job split still buys: the job that
    classifies holds every write scope EXCEPT the one that can merge."""
    governance = _code(_rendered().read_text(_GOVERNANCE) or "")
    gate, arm = governance.split("\n  arm-auto-merge:")
    assert "contents: read" in gate
    assert "contents: write" not in gate
    assert "contents: write" in arm


def test_the_arm_job_never_persists_its_write_scoped_token() -> None:
    """WF-06. This job now checks out, and it holds `contents: write` — the
    default `persist-credentials: true` would put that token in `.git/config`
    for every later step to inherit through plain `git`, dependency resolution
    included."""
    arm = _code(_rendered().read_text(_GOVERNANCE) or "").split("\n  arm-auto-merge:")[1]
    assert "persist-credentials: false" in arm


def test_the_label_lane_passes_the_completed_runs_head_explicitly() -> None:
    """`label-failed-run` falls back to `$GITHUB_SHA` when `--head-sha` is
    omitted — and on a `workflow_run` event that variable names the DEFAULT
    BRANCH's head, not the head of the run that just failed. The fallback would
    look up an entirely unrelated commit, so this lane must pass the value, and
    must pass it through an env var rather than `run:` interpolation."""
    text = _rendered().read_text(f"{render.GITHUB_DIR}/pr-checks-label.yml") or ""
    assert "HEAD_SHA: ${{ github.event.workflow_run.head_sha }}" in text
    assert '--head-sha "$HEAD_SHA"' in _code(text)


def test_no_generated_workflow_interpolates_an_expression_into_a_run_line() -> None:
    """ADR-4 BD-4: every value a `run:` line consumes arrives through an env
    var. None of these is untrusted today — a PR number, a runner-set sha, a
    bot-authored disposition from a closed set — and the discipline is what
    keeps that true after the next edit adds a field that is neither."""
    files = _rendered(deploy_workflow="render-deploy.yml")
    for path in files.files:
        for step in _steps(files.read_text(path) or ""):
            assert "${{" not in step, f"{path}: {step}"


def test_no_generated_workflow_uses_a_third_party_action_beyond_setup() -> None:
    """`actions/stale` was the last one, and it left with the stale lane:
    GitHub-only, so GitLab could never have had the same behaviour, and a
    third-party action inside a job holding `pull-requests: write`. What
    remains is `checkout`, the deployment's own `ci.setup`, and harden-runner
    on the one scheduled lane that reaches the network."""
    files = _rendered()
    used = {
        line.split("uses:", 1)[1].split("@")[0].strip()
        for path in files.files
        for line in _code(files.read_text(path) or "").split("\n")
        if line.strip().startswith(("uses:", "- uses:"))
    }
    assert used == {
        "actions/checkout",
        "./.github/actions/setup-bot",
        "step-security/harden-runner",
    }


def test_every_scheduled_job_carries_the_cron_upstream_guard() -> None:
    """A fork inherits every schedule in a workflow file and runs it off its
    own stale copy."""
    files = _rendered(owner="acme")
    for name in ("reconcile.yml", "stale.yml"):
        text = files.read_text(f"{render.GITHUB_DIR}/{name}") or ""
        assert "schedule:" in text
        assert "if: github.repository_owner == 'acme'" in text


def test_every_workflow_declares_default_deny_permissions() -> None:
    files = _rendered()
    for name in (
        "validate.yml",
        "governance.yml",
        "reconcile.yml",
        "pr-checks-label.yml",
        "stale.yml",
    ):
        assert "\npermissions: {}\n" in (files.read_text(f"{render.GITHUB_DIR}/{name}") or "")


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    files = _rendered()
    rendered = {path: files.read_text(path) or "" for path in files.files}
    for text in rendered.values():
        for line in text.split("\n"):
            if "uses:" in line and "@" in line and "./" not in line:
                ref = line.split("@")[1].split("#")[0].strip()
                assert len(ref) == 40, f"not a commit SHA: {line.strip()}"


def test_the_gitlab_file_never_owns_the_root_pipeline() -> None:
    files = _rendered(forge="gitlab")
    assert set(files.files) == {".gitlab-ci/indexbot.yml"}


def test_the_gitlab_poll_job_checks_nothing_out() -> None:
    """The GitLab counterpart of the untrusted-PR-data-only contract: the
    privileged lane must be structurally incapable of running MR content."""
    text = _rendered(forge="gitlab").read_text(".gitlab-ci/indexbot.yml") or ""
    poll = text.split("indexbot-governance-poll:")[1].split("indexbot-reconcile:")[0]
    assert "GIT_STRATEGY: none" in poll
    assert 'CI_PROJECT_NAMESPACE == "ocx-sh"' in poll


# --- --check -----------------------------------------------------------------


def test_check_is_clean_on_freshly_rendered_files() -> None:
    files = _rendered()
    assert _run(files, check=True) is ExitCode.OK


def test_check_reports_a_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    files = _rendered()
    del files.files[_VALIDATE]

    assert _run(files, check=True) is ExitCode.VALIDATION_FAILURE
    assert "is missing" in capsys.readouterr().err


def test_check_catches_a_hand_edited_trigger(capsys: pytest.CaptureFixture[str]) -> None:
    """The edit this gate exists for: moving the unprivileged half onto the
    privileged trigger."""
    files = _rendered()
    text = files.read_text(_VALIDATE) or ""
    files.write_text(
        _VALIDATE, text.replace("\non:\n  pull_request:\n", "\non:\n  pull_request_target:\n")
    )

    assert _run(files, check=True) is ExitCode.VALIDATION_FAILURE
    assert "does not match" in capsys.readouterr().err


def test_check_tolerates_a_re_pinned_action() -> None:
    """A deployment's dependency bot bumps action SHAs. Red on every such PR
    would train the gate to be ignored."""
    files = _rendered()
    text = files.read_text(_VALIDATE) or ""
    old = text.split("uses: actions/checkout@")[1].split("\n")[0].split(" ")[0]
    files.write_text(_VALIDATE, text.replace(old, "f" * 40))

    assert _run(files, check=True) is ExitCode.OK


def test_a_re_pinned_action_is_carried_forward_into_the_next_render() -> None:
    """The other half of the same argument: the bump must survive a render,
    or `indexbot ci` would silently revert it."""
    files = _rendered()
    text = files.read_text(_VALIDATE) or ""
    old = text.split("uses: actions/checkout@")[1].split("\n")[0].split(" ")[0]
    for path in list(files.files):
        files.write_text(path, (files.read_text(path) or "").replace(old, "f" * 40))

    assert _run(files) is ExitCode.OK
    assert f"actions/checkout@{'f' * 40}" in (files.read_text(_VALIDATE) or "")


def test_a_file_rendered_by_a_newer_tool_is_refused_not_overwritten() -> None:
    """The operator's bot is older than whatever last rendered their pipeline.
    Downgrading it silently is the one outcome worse than doing nothing."""
    files = _rendered()
    text = files.read_text(_VALIDATE) or ""
    files.write_text(
        _VALIDATE, text.replace(render.HEADER_LINE, "# generated by indexbot ci v99 — do not edit")
    )

    with pytest.raises(ValidationError, match="newer than this bot"):
        _run(files)


def test_check_fails_on_a_github_workflow_orphaned_by_a_forge_flip_to_gitlab(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`build_render_plan` plans exactly one forge's file set, so `existing`
    (scoped to the current plan's paths) never even reads the five GitHub
    workflows once `ci.forge` flips to `gitlab` — they drop out of the drift
    comparison entirely while staying committed and still executing. `--check`
    must notice them by scanning for the header directly, not just diffing
    the current plan."""
    files = _rendered()  # forge="github" (the default): renders the five workflows

    assert _run(files, check=True, forge="gitlab") is ExitCode.VALIDATION_FAILURE
    err = capsys.readouterr().err
    assert _VALIDATE in err
    assert "ci.forge no longer plans it" in err


def test_check_reports_only_the_orphan_message_when_the_current_plan_is_clean(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The closing "run `indexbot ci` and commit" line is drift-specific
    advice — running the tool again cannot delete an orphan (write mode only
    ever writes `plan` entries, never deletes), so it must not print when the
    only failure is an orphan and the current plan has no drift at all."""
    files = _rendered(forge="gitlab")
    files.write_text(_VALIDATE, f"{render.HEADER_LINE}\nname: validate\n")

    assert _run(files, check=True, forge="gitlab") is ExitCode.VALIDATION_FAILURE
    err = capsys.readouterr().err
    assert "ci.forge no longer plans it" in err
    assert "run `indexbot ci`" not in err


def test_check_ignores_an_unplanned_file_with_no_generated_header(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hand-written workflow living beside the generated ones (`ci.yml`,
    `render-deploy.yml`) is not this tool's problem — only a committed file
    that still claims to be generated is reported as orphaned."""
    files = _rendered(forge="gitlab")
    files.write_text(_VALIDATE, "name: hand-written\non:\n  push:\n")

    assert _run(files, check=True, forge="gitlab") is ExitCode.OK
    assert "ci.forge no longer plans it" not in capsys.readouterr().err


def test_a_hand_written_tree_is_drift_on_the_first_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No header means "not ours" — reported, then overwritten on write. That
    is exactly what adopting the generator should look like."""
    files = InMemoryFiles(files={_VALIDATE: b"name: validate\non:\n  pull_request:\n"})

    assert _run(files, check=True) is ExitCode.VALIDATION_FAILURE
    assert "does not match" in capsys.readouterr().err
    assert _run(files) is ExitCode.OK
    assert (files.read_text(_VALIDATE) or "").startswith(render.HEADER_LINE)


# --- the GitLab job file must run under the shell it will actually get -------


def test_the_gitlab_scripts_are_posix_sh() -> None:
    """GitLab runs `script:` through whatever shell the image provides, and
    the default one is Alpine — busybox ash. `mapfile`, `<<<` and arrays all
    parse fine locally and fail in the job, which is the worst place to learn
    it. A `sh -n` parse of every script block is the cheap version of that
    lesson."""
    text = _rendered(forge="gitlab").read_text(".gitlab-ci/indexbot.yml") or ""
    for bashism in ("mapfile", "<<<", "pipefail", "=()"):
        for line in text.split("\n"):
            code = line.split("#", 1)[0]
            assert bashism not in code, f"bash-only construct {bashism!r} in: {line.strip()}"


def test_the_gitlab_jobs_install_git_before_using_it() -> None:
    """Both the merge-base diff and `uvx --from git+…` need it, and the
    default image ships without."""
    text = _rendered(forge="gitlab").read_text(".gitlab-ci/indexbot.yml") or ""
    assert "command -v git" in text


# --- registry credentials: rendered passthrough ------------------------------


def _credentialed_policy(**overrides: object) -> IndexPolicy:
    return make_policy(
        registries={
            "ghcr.io": RegistryConfig(host="ghcr.io"),
            "artifactory.corp": RegistryConfig(
                host="artifactory.corp", credentials_env="OCX_REGISTRY_ART"
            ),
        },
        **overrides,
    )


def test_a_credentialed_registry_reaches_only_the_reconcile_job() -> None:
    """The privileged lane is the one that verifies registry truth, so it is
    the one that gets the secret. The fork gate runs a pull request's own head
    code and must never see it."""
    plan = render.build_render_plan(_credentialed_policy(), existing={})

    reconcile = plan[".github/workflows/reconcile.yml"]
    assert "          OCX_REGISTRY_ART: ${{ secrets.OCX_REGISTRY_ART }}" in reconcile
    assert "OCX_REGISTRY_ART" not in plan[".github/workflows/validate.yml"]
    assert "OCX_REGISTRY_ART" not in plan[".github/workflows/governance.yml"]


def test_no_credentialed_registry_renders_no_env_line() -> None:
    """An index with only anonymous registries renders exactly what it
    rendered before this existed — the empty placeholder line is dropped
    rather than left blank."""
    plan = render.build_render_plan(make_policy(), existing={})
    reconcile = plan[".github/workflows/reconcile.yml"]

    assert "secrets." not in reconcile
    assert "\n\n        run:" not in reconcile


def test_every_declared_variable_is_rendered_once_and_sorted() -> None:
    """Two registries sharing one variable name is one secret, and the order
    is the sorted one so the rendered file is byte-stable."""
    policy = make_policy(
        registries={
            "a.corp": RegistryConfig(host="a.corp", credentials_env="Z_CREDS"),
            "b.corp": RegistryConfig(host="b.corp", credentials_env="A_CREDS"),
            "c.corp": RegistryConfig(host="c.corp", credentials_env="Z_CREDS"),
        }
    )
    reconcile = render.build_render_plan(policy, existing={})[".github/workflows/reconcile.yml"]
    lines = [line.strip() for line in reconcile.splitlines() if "secrets." in line]

    assert lines == [
        "A_CREDS: ${{ secrets.A_CREDS }}",
        "Z_CREDS: ${{ secrets.Z_CREDS }}",
    ]


def test_gitlab_renders_the_variable_as_an_operator_note_only() -> None:
    """GitLab has no `secrets.` context: the variable is a masked, protected
    project variable that is simply ambient to the scheduled job, exactly as
    `$GITLAB_TOKEN` already is. Writing it into `variables:` would put a
    credential-shaped name on a file whose merge-request job a fork runs
    (GL-03)."""
    plan = render.build_render_plan(_credentialed_policy(forge="gitlab"), existing={})
    pipeline = plan[".gitlab-ci/indexbot.yml"]

    assert "Settings > CI/CD > Variables > OCX_REGISTRY_ART" in pipeline
    assert "OCX_REGISTRY_ART:" not in pipeline
