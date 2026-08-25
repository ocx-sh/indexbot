"""Behavioral assertions over the rendered GitLab template, executed under a
real POSIX shell rather than keyword-scanned.

This file used to carry two more, and losing them is the point of the change
that removed them. `indexbot-validate` was ~60 lines of `sh` and both tests
replayed a *shell bug* it had shipped: A-3, where the reserved-namespace
carve-out compared the source project against `$CI_PROJECT_PATH` — which for a
fork merge request's own pipeline IS the fork, so the check passed for every
merge request and a fork could claim a segment the index reserves for its own
brand; and D-9, where an unquoted `for file in $changed` word-split a path
containing a space and glob-expanded one containing `*`.

Neither shape exists any more: the job is `indexbot validate-pr`, and both
rules are Python with named tests — `tests/cli/test_validate_pr.py`'s
`test_a_fork_gitlab_merge_request_may_not_claim_a_reserved_segment` for
A-3, and its `_materialize_base` / path-handling set for D-9. Those
tests assert the rule; the ones below assert what remains a property of the
generated YAML itself: that it parses under the shell GitLab will actually
give it, and that each job is one command rather than a shell program.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess

import pytest

from ocx_indexbot.ci import render
from ocx_indexbot.cli import ci_cmd
from ocx_indexbot.core.policy import resolves_at_runtime
from ocx_indexbot.core.workflow_invariants import check_workflows
from ocx_indexbot.exit_codes import ExitCode
from tests.fakes import InMemoryFiles, make_policy

_DASH = shutil.which("dash")
_SKIP_NO_DASH = pytest.mark.skipif(
    _DASH is None, reason="behavioral checks execute the shipped script under a real POSIX shell"
)


def _rendered(**overrides: object) -> str:
    """The `.gitlab-ci/indexbot.yml` a real `indexbot ci` run produces.

    `overrides` reach `make_policy`, for the one property that is only visible
    on a deployment declaring no `ci.setup` of its own — the default image.
    """
    files = InMemoryFiles()
    policy = make_policy(forge="gitlab", **overrides)
    assert ci_cmd.run(argparse.Namespace(check=False), files=files, policy=policy) is ExitCode.OK
    text = files.read_text(render.GITLAB_FILE)
    assert text is not None
    return text


def _prose(text: str) -> str:
    """Every comment line in `text`, unwrapped into one whitespace-normalized
    string.

    A YAML comment carries its own line breaks, so asserting on a sentence
    means asserting on where it happened to wrap. This drops that: the
    assertion is about what the generated file TELLS an operator, and a
    reflow is not a change to that.
    """
    words = " ".join(
        line.strip().lstrip("#") for line in text.split("\n") if line.strip().startswith("#")
    )
    return " ".join(words.split())


def _rendered_github(name: str) -> str:
    """One `.github/workflows/<name>` a real `indexbot ci` run produces."""
    files = InMemoryFiles()
    assert (
        ci_cmd.run(argparse.Namespace(check=False), files=files, policy=make_policy())
        is ExitCode.OK
    )
    text = files.read_text(f"{render.GITHUB_DIR}/{name}")
    assert text is not None
    return text


def test_the_github_arm_is_bound_to_the_head_the_gate_judged() -> None:
    """Regression for a Block finding, and the finding is about REACH: the
    adapters have bound arming to the gated revision all along
    (`expectedHeadOid` on GitHub, `sha` on GitLab), and on GitHub that binding
    had zero production callers — the template ran `classify-pr` and
    `governance-check` and then armed with a bare
    `gh pr merge --auto --squash`. So the guard was live only on GitLab, while
    `ocx-sh/index` is a `forge: github` deployment.

    The attack it leaves open: a PR author who owns the touched root opens a
    refresh, the gate greens head A, the author pushes B before the arm step
    runs, and `--auto` arms B — which nothing classified, validated or gated.

    Asserted on the rendered TEMPLATE, not on the Python, because that is the
    whole shape of the finding: a correct adapter no YAML calls is not a
    control. `--head-sha "$HEAD_SHA"` reaching the command is what makes it
    one, and `$HEAD_SHA` must come from the event payload rather than a live
    read — a head fetched at arm time is by definition not the gated one."""
    governance = _rendered_github("governance.yml")
    code = "\n".join(line for line in governance.split("\n") if not line.strip().startswith("#"))

    assert '--arm-only --disposition "$DISPOSITION" --head-sha "$HEAD_SHA"' in code
    assert "HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in code
    # The `gh pr merge` this replaced must not come back in any form: an
    # unbound `--auto` is exactly the finding, and the bound fallback merge is
    # the adapter's job now (`_squash_merge`, pinned via the REST `sha`).
    assert "gh pr merge" not in code


def test_the_rendered_github_tree_passes_every_workflow_invariant() -> None:
    """The generator and the auditor are two halves of one control, and this is
    the seam. `indexbot ci` renders the privileged lane; `indexbot
    workflows-check` is what an index repo runs over it in CI. Nothing before
    this test made them agree — WF-08 was written against a planted fixture,
    and a rule the generator itself violates is a rule that gets deleted rather
    than obeyed.

    It also pins the whole set rather than WF-08 alone: the arm job's
    `contents: write`, its `persist-credentials: false`, the absence of any
    `ref:`, the SHA pins and the cron guards are each somebody's earlier
    finding, and a re-render that quietly drops one should fail here."""
    files = InMemoryFiles()
    assert (
        ci_cmd.run(argparse.Namespace(check=False), files=files, policy=make_policy())
        is ExitCode.OK
    )
    tree = {
        path.rsplit("/", 1)[1]: text
        for path in files.list_files(render.GITHUB_DIR)
        if (text := files.read_text(path)) is not None
    }
    assert len(tree) == 5, tree.keys()
    assert check_workflows(tree, owner="ocx-sh") == ()


def test_the_github_arm_job_runs_a_pinned_bot() -> None:
    """WF-08 at the one call site it exists for. `arm-auto-merge` holds a token
    that can move an unprotected base branch and squash-merge a pull request,
    and until `ci.run` lost its `uvx ocx-indexbot` default, the version it ran
    was whatever PyPI held when the step started.

    Asserted on the rendered YAML for the same reason the head-SHA binding
    above is: the refusal lives in `build_render_plan` and the audit rule in
    `core/workflow_invariants.py`, and neither is a control until the bytes a
    deployment actually commits carry the pin."""
    # On the job header, never the file header's prose about it — that comment
    # names `arm-auto-merge:` too, and splitting on the bare name lands between
    # the two.
    arm = _rendered_github("governance.yml").split("\n  arm-auto-merge:\n")[1]
    run_line = next(
        line for line in arm.split("\n") if line.strip().startswith("run:") and "--arm-only" in line
    )
    assert "--frozen" in run_line, run_line
    assert not resolves_at_runtime(run_line)


def _script_blocks(text: str) -> list[str]:
    """Every `script:`/`before_script:` list item in `text`, as plain shell —
    block scalars (`- |`) dedented, one-line items verbatim.

    Not a YAML parser: this project deliberately carries none (see
    `core/maintainers.py`, `cli/seed_import.py`). Just the one indentation
    rule these generated files are built on — a key's list items sit two
    spaces in, and a literal block scalar's content sits two spaces past
    that, until the first line that isn't.
    """
    lines = text.split("\n")
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() not in ("script:", "before_script:"):
            i += 1
            continue
        key_indent = len(lines[i]) - len(lines[i].lstrip(" "))
        i += 1
        while i < n:
            item = lines[i]
            if item.strip() == "":
                i += 1
                continue
            item_indent = len(item) - len(item.lstrip(" "))
            if item_indent <= key_indent:
                break
            stripped_item = item.strip()
            if stripped_item.startswith("#"):
                i += 1
                continue
            body = stripped_item[2:]  # past "- "
            if body != "|":
                blocks.append(body)
                i += 1
                continue
            block_indent = item_indent + 2
            i += 1
            block_lines: list[str] = []
            while i < n and (
                lines[i].strip() == "" or len(lines[i]) - len(lines[i].lstrip(" ")) >= block_indent
            ):
                block_lines.append(lines[i][block_indent:] if lines[i].strip() else "")
                i += 1
            blocks.append("\n".join(block_lines))
    return blocks


@_SKIP_NO_DASH
def test_the_gitlab_scripts_parse_as_posix_sh() -> None:
    """`dash -n` — not a keyword scan — over every `script:`/`before_script:`
    block. GitLab runs these under whatever shell the image provides, and the
    default is Alpine's busybox ash; `dash` is the standard stand-in for that
    check, and unlike a plain `sh` it cannot silently resolve to bash and
    accept a bashism (`mapfile`, `<<<`, arrays) the target shell would
    reject."""
    assert _DASH is not None  # narrows for pyright; guaranteed by _SKIP_NO_DASH
    for block in _script_blocks(_rendered()):
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell, rendered-template text
            [_DASH, "-n", "-c", block], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{result.stderr.strip()} in:\n{block}"


def test_every_gitlab_job_runs_exactly_one_indexbot_command() -> None:
    """The contract this generator now owes a hand-written pipeline: read one
    job, copy one command. Every `script:` here is a single `indexbot`
    invocation — no `jq` filter, no `git` call, no loop, no exit-code `case`.

    Asserted on the rendered file rather than trusted, because the failure mode
    is gradual: one `||` for a special case, then a second, and the security
    argument is back in YAML where it cannot be tested. `before_script`'s
    conditional `apk add git` is the deliberate exception — provisioning the
    image is not the lane's logic — and is excluded by name.
    """
    text = _rendered()
    before_script = _script_blocks(text.split("indexbot-validate:")[0])
    jobs = [block for block in _script_blocks(text) if block not in before_script]

    assert len(jobs) == 5, "one script block per generated job"
    for block in jobs:
        assert "\n" not in block.strip(), f"more than one line of shell:\n{block}"
        assert block.startswith("uv run --project bot-tools --frozen -- indexbot "), block
        for shell_ism in ("|", "&&", ";", "$(", "`", "case ", "for ", "if "):
            assert shell_ism not in block, f"{shell_ism!r} in: {block}"


def test_the_gitlab_lanes_cover_every_command_the_github_lane_runs() -> None:
    """The two forges must run the same bot, not similar ones. GitHub reaches
    `governance-gate` from a privileged pull-request trigger and GitLab reaches
    the same decision from a scheduled `governance-poll` — that asymmetry is
    forced (see the file's own header) — but every other lane has to exist on
    both, or a GitLab deployment silently runs less governance than a GitHub
    one."""
    scripts = _script_blocks(_rendered())
    for command in ("validate-pr", "reconcile --anomaly-ok", "label-failed-run", "stale"):
        assert any(block.endswith(f"indexbot {command}") for block in scripts), command


def test_every_scheduled_gitlab_lane_is_upstream_only() -> None:
    """A fork does not inherit a GitLab schedule — they are project settings —
    but a fork whose owner creates one would otherwise gate merge requests,
    file anomaly issues and stale-close pull requests against its own stale
    copy of this file. The guard is per-job because the schedules are."""
    text = _rendered()
    for job in ("indexbot-governance-poll", "indexbot-reconcile", "indexbot-stale"):
        block = text.split(f"\n{job}:\n")[1].split("\nindexbot-")[0]
        assert '$CI_PIPELINE_SOURCE == "schedule"' in block, job
        assert '$CI_PROJECT_NAMESPACE == "ocx-sh"' in block, job


def test_the_label_failed_run_lane_never_runs_tokenless_in_a_fork() -> None:
    """FP-8's scope is fork merge requests, and GitLab cannot reach it: a fork
    MR's pipeline runs IN THE FORK, under the fork's variables, with no
    `$GITLAB_TOKEN` — the same boundary that makes `governance-poll` a schedule
    rather than an MR job. So the rule requires the pipeline to be running in
    the TARGET project, which is where the parent's token exists.

    Without that condition the job would fail tokenless on every fork MR
    instead of labeling anything; with it, the lane works exactly when the
    project enables "run pipelines in the parent project for merge requests
    from forks" and no-ops otherwise. `$CI_PROJECT_PATH` is the right left-hand
    side here for the same reason it was the WRONG one in A-3 — it names where
    the pipeline is running, which is precisely the question being asked."""
    block = _rendered().split("\nindexbot-label-failed-run:\n")[1].split("\nindexbot-")[0]
    assert "$CI_PROJECT_PATH == $CI_MERGE_REQUEST_PROJECT_PATH" in block
    assert "when: on_failure" in block
    # `.post`, or `on_failure` could never fire: it means "a job in an EARLIER
    # stage failed", and `indexbot-validate` sits in the default stage.
    assert "stage: .post" in block


def test_the_default_gitlab_image_is_digest_pinned() -> None:
    """`indexbot-governance-poll` holds `$GITLAB_TOKEN` (`api` scope) and runs
    in whatever `default: image:` names. A tag is a mutable pointer its
    publisher can move under a deployment that changed nothing, which is the
    same hazard WF-02 pins every GitHub `uses:` against — and there is no
    GitLab-side `workflows-check` to catch it after the fact, so the default
    has to be right at render time.

    Asserted on the rendered file rather than on the constant, because the
    constant is only a control once it reaches the `image:` line."""
    image_line = next(
        line for line in _rendered(setup="").split("\n") if line.strip().startswith("image:")
    )
    assert "@sha256:" in image_line, image_line
    assert ":python3" not in image_line, "a tag in the image ref is exactly the finding"


def test_the_gitlab_fork_pipeline_setting_is_documented_as_a_token_handover() -> None:
    """The `label-failed-run` guard fails safe on its own, so what is left to
    get wrong is the prose: an operator who wants the lane working reads the
    comment, enables "run pipelines in the parent project for merge requests
    from forks", and thereby runs fork-authored `.gitlab-ci.yml` in the parent
    with the `api`-scoped token in scope for every job.

    The generated file is where that operator is standing when they decide, so
    the warning has to be in the generated file — not only in the docs."""
    block = _rendered().split("\nindexbot-label-failed-run:\n")[0]
    warning = _prose(block.split("# FP-8 spam posture, first half")[1])

    assert "run pipelines in the parent project" in warning
    assert "fork-authored" in warning
    assert "$GITLAB_TOKEN" in warning
    assert "Do not enable it for this lane" in warning


def test_the_validate_workflow_says_its_policy_comes_from_the_base_ref() -> None:
    """The job checks out PR-head content, so the policy file under that root
    is the pull request's own — and `name_segments` in it picks the pathspec
    that decides what gets validated at all. `cli/validate_pr.py` reads the
    base ref's copy instead, and the rendered workflow is where the next
    person editing this lane will look for the reason it does."""
    prose = _prose(_rendered_github("validate.yml"))

    assert "the deployment policy this gate obeys, taken from the BASE ref" in prose
    assert "reads that file from" in prose and "the BASE ref through git" in prose
    # Two claims that were in earlier cuts of this comment and are now wrong.
    # `governance.yml` gates the machine lane from base-ref data regardless, so
    # a green context was never the only thing stopping a fork; and refusing a
    # pull request that touches the policy inverted the very control that keeps
    # the file committed rather than a settings-page value.
    assert "that context is the only thing stopping a fork" not in prose
    assert "byte-compares the head's copy against the base ref's" not in prose


def test_only_the_validate_lane_checks_anything_out() -> None:
    """`GIT_STRATEGY: none` is the GitLab spelling of the untrusted-PR-data-only
    contract, and reconcile is the one privileged lane that legitimately needs
    a checkout — it verifies the committed `p/**` tree against the registry.
    Every other privileged lane reaches the forge API and nothing else, and
    says so structurally rather than by convention."""
    text = _rendered()

    def block(job: str) -> str:
        """One job's YAML, comments stripped — the reason `GIT_STRATEGY: none`
        is or is not there is written out at length beside it."""
        body = text.split(f"\n{job}:\n")[1].split("\nindexbot-")[0]
        return "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))

    for job in ("indexbot-governance-poll", "indexbot-label-failed-run", "indexbot-stale"):
        assert "GIT_STRATEGY: none" in block(job), job
    assert "GIT_STRATEGY: none" not in block("indexbot-reconcile")
    assert "GIT_DEPTH: 0" in block("indexbot-validate"), "validate-pr diffs against the merge base"
