"""Static-assertion workflow-security suite (spec X7, register §5).

Covers G-14 (`permissions:` default-deny + SHA-pinned `uses:` across every
workflow), G-16/FP-7 (the privileged `pull_request_target` governance job
never checks out PR head), the `contents: write` split that lets
`governance.yml` arm auto-merge at all (the write token lives in a job that
checks nothing out, and never in the job that runs `bot/`'s source), and the
trigger split that keeps a `pull_request_target` run from ever emitting a
check run named after a branch-protection-required context. The repo has no
runtime YAML dependency and the credential process must gain none, so these
tests hand-parse the specific keys (`permissions:`, `uses:`, `ref:`, `on:`)
with the stdlib only — a line scan, never a YAML library import.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
_VALIDATE = _WORKFLOWS_DIR / "validate.yml"
_GOVERNANCE = _WORKFLOWS_DIR / "governance.yml"

_PERMISSIONS_DEFAULT_DENY_RE = re.compile(r"(?m)^permissions:\s*\{\}\s*$")
_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TRIGGER_RE = re.compile(r"^  ([a-z_]+):")


def _workflow_files() -> list[Path]:
    return sorted(_WORKFLOWS_DIR.glob("*.yml"))


def _triggers(text: str) -> set[str]:
    """The event names in a workflow's `on:` block — the two-space mapping
    keys between `on:` and the next top-level key. Comment lines (the zizmor
    suppressions live in there) are indented but never match `^  <name>:`
    because they start with `#`."""
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.rstrip() == "on:")
    events: set[str] = set()
    for line in lines[start + 1 :]:
        if re.match(r"^\S", line):
            break
        if match := _TRIGGER_RE.match(line):
            events.add(match.group(1))
    return events


def _uses_refs(text: str) -> list[str]:
    return [match.group(1) for line in text.splitlines() if (match := _USES_RE.match(line))]


def _grant(job_text: str, permission: str) -> bool:
    """A real `permissions:` grant, not prose. Every assertion here scans raw
    job text, comments included, so a positive `in` check is satisfiable by a
    comment that merely *names* the grant — mutation-proved: deleting the real
    `contents: write` line left an earlier revision of this suite green,
    because the job's own comment explains it. Anchor on the six-space
    mapping key instead."""
    return re.search(rf"(?m)^\s{{6}}{re.escape(permission)}\s*(?:#.*)?$", job_text) is not None


def _runs(job_text: str, pattern: str) -> bool:
    """A `gh` command a `run:` block actually executes, not prose that quotes
    it — same mutation hazard as `_grant`. The lookahead excludes comment
    lines; the command may be bare or inside a `$(...)` capture."""
    return re.search(rf"(?m)^(?!\s*#).*\bgh {pattern}", job_text) is not None


def _job_block(text: str, job: str) -> str:
    """One job's YAML text — from its `  <job>:` header (exactly two-space
    indent) to the next two-space job header or EOF."""
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.rstrip() == f"  {job}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


# --- G-14 ------------------------------------------------------------------


def test_every_workflow_has_top_level_permissions_default_deny() -> None:
    """G-14: every `.github/workflows/*.yml` declares top-level
    `permissions: {}` (default-deny; jobs elevate per-job)."""
    workflows = _workflow_files()
    assert workflows, "expected at least one workflow to audit"
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert _PERMISSIONS_DEFAULT_DENY_RE.search(text), (
            f"{workflow.name}: no top-level permissions: {{}}"
        )


def test_every_workflow_uses_is_sha_pinned() -> None:
    """G-14: every marketplace `uses:` across every workflow is pinned to a
    40-hex commit SHA. Local `./` composite actions and `docker://` refs are
    exempt — neither is a pinnable marketplace ref."""
    for workflow in _workflow_files():
        for ref in _uses_refs(workflow.read_text(encoding="utf-8")):
            if ref.startswith(("./", "docker://")):
                continue
            _, _, pin = ref.partition("@")
            assert _SHA_RE.fullmatch(pin), f"{workflow.name}: {ref!r} is not a 40-hex SHA pin"


# --- trigger split (skipped-check-run collision) ---------------------------


def test_no_workflow_declares_both_pr_triggers() -> None:
    """`pull_request` and `pull_request_target` both fire on the SAME PR head
    commit, so a workflow carrying both must discriminate them with a
    job-level `if: github.event_name == ...` — and a job skipped by such an
    `if:` STILL emits a check run, conclusion `skipped`, under its own name.
    GitHub counts a `skipped` conclusion as satisfying a required status check
    and resolves duplicate-named contexts to the most recent one, so the
    privileged run publishes a green-equivalent impostor of whatever required
    context the unprivileged half owns (live-observed on PR #70's head
    1d7a9b4e: two `schema-validate-pr` check runs, one `skipped`, one
    `success`). Keep the two triggers in separate files."""
    for workflow in _workflow_files():
        events = _triggers(workflow.read_text(encoding="utf-8"))
        assert not {"pull_request", "pull_request_target"} <= events, (
            f"{workflow.name}: declares both PR triggers - a trigger-discriminating"
            " job `if:` emits skipped check runs under the other half's context name"
        )


def test_required_pr_diff_context_lives_in_a_pull_request_only_workflow() -> None:
    """`schema-validate-pr` is on `main`'s required-context list and is the
    sole enforcement point for ND-4's reserved-brand gate. It must live in a
    workflow whose only trigger is `pull_request`, and no other workflow may
    define a job by that name."""
    assert _triggers(_VALIDATE.read_text(encoding="utf-8")) == {"pull_request"}
    others = [w for w in _workflow_files() if w != _VALIDATE]
    for workflow in others:
        assert "\n  schema-validate-pr:" not in workflow.read_text(encoding="utf-8")


def test_no_job_if_discriminates_on_the_event_name_in_the_pr_workflows() -> None:
    """The structural half of the fix: with one trigger per file there is
    nothing for a `github.event_name` guard to decide, and reintroducing one
    is how the skipped-check-run collision comes back. Matched on `if:` lines
    only — the file headers narrate the hazard and name the expression."""
    for workflow in (_VALIDATE, _GOVERNANCE):
        text = workflow.read_text(encoding="utf-8")
        assert not re.search(r"(?m)^\s+if:.*github\.event_name", text), (
            f"{workflow.name}: a job `if:` discriminates on github.event_name"
        )


# --- G-16 / FP-7 -----------------------------------------------------------


def test_pull_request_target_governance_job_never_checks_out_pr_head() -> None:
    """G-16/FP-7: `governance.yml`'s privileged `governance-gate` job runs
    under `pull_request_target`, checks out the base ref only (no `ref:` key),
    never resolves `github.event.pull_request.head`, and holds no PAT secret —
    the untrusted PR-head content never runs in the credentialed job."""
    text = _GOVERNANCE.read_text(encoding="utf-8")
    assert _triggers(text) == {"pull_request_target"}
    privileged = _job_block(text, "governance-gate")
    assert "actions/checkout@" in privileged
    # No `ref:` key — a checkout with no ref defaults to the base branch tip,
    # never PR head (the only way to check out head is an explicit `ref:`
    # resolving `pull_request.head`).
    assert not re.search(r"(?m)^\s*ref:\s", privileged)
    assert "secrets." not in privileged


def test_unprivileged_pr_head_job_holds_no_secrets() -> None:
    """G-16/FP-7 counterpart: `validate.yml`'s `schema-validate-pr` job — the
    one that checks out PR head — runs on the unprivileged `pull_request`
    trigger and references no secrets (GitHub strips them for fork PRs)."""
    text = _VALIDATE.read_text(encoding="utf-8")
    assert _triggers(text) == {"pull_request"}
    unprivileged = _job_block(text, "schema-validate-pr")
    assert "github.event.pull_request.head.sha" in unprivileged
    assert "secrets." not in unprivileged


# --- contents: write split -------------------------------------------------


def test_governance_gate_never_holds_contents_write() -> None:
    """`governance-gate` checks out and runs `bot/`'s source and forwards
    `github.token` to a third-party action via `setup-bot`. It therefore
    holds `contents: read` and must never be "simplified" into holding the
    write token that arming auto-merge needs."""
    text = _GOVERNANCE.read_text(encoding="utf-8")
    privileged = _job_block(text, "governance-gate")
    assert _grant(privileged, "contents: read")
    assert not _grant(privileged, "contents: write")


def test_arm_auto_merge_job_never_checks_out() -> None:
    """`arm-auto-merge` is the only job holding `contents: write` — arming and
    withdrawing auto-merge are deferred writes to the base branch. That is
    only safe while the job runs no repository code: no `uses:` of any kind
    (so no checkout and no `setup-bot`) and no secret. It arms off
    `governance-gate`'s ownership-checked `disposition`, and the same output
    drives a `--disable-auto` branch, because arming a PR whose head later
    moves outside its author's owned roots must be revocable."""
    text = _GOVERNANCE.read_text(encoding="utf-8")
    arm = _job_block(text, "arm-auto-merge")
    assert _grant(arm, "contents: write")
    assert not _uses_refs(arm)
    assert "secrets." not in arm
    assert "needs: governance-gate" in arm
    assert re.search(r"(?m)^\s+if:.*disposition == 'success'", arm)
    assert re.search(r"(?m)^\s+if:.*disposition != 'success'", arm)
    assert _runs(arm, r"pr merge .*--auto --squash")
    assert _runs(arm, r"pr merge .*--disable-auto")


def test_arm_auto_merge_withdrawal_is_fail_closed() -> None:
    """The withdrawal must fail loudly. Reading `autoMergeRequest` first is
    what separates "was never armed" (the ordinary human lane) from "the
    disable call was denied or errored" — guarding `--disable-auto` with `||`
    instead conflates them, leaving an armed PR armed behind a green check and
    a notice that asserts the opposite. The job also must not inherit the
    default `success()` of its `needs:`, or a governance-gate that errors
    skips the withdrawal entirely."""
    text = _GOVERNANCE.read_text(encoding="utf-8")
    arm = _job_block(text, "arm-auto-merge")
    assert _runs(arm, r"pr view .*--json autoMergeRequest")
    assert not re.search(r"(?m)^(?!\s*#).*\bgh pr merge[^\n]*--disable-auto[^\n]*\|\|", arm)
    assert re.search(r"(?m)^\s+if:.*!cancelled\(\)", arm)
    # Serialized per PR: an arm job from an older head must never execute
    # after the withdrawal triggered by a newer one.
    assert re.search(r"(?m)^\s+group: arm-auto-merge-", arm)


# --- #67: the machine lane's deploy trigger --------------------------------


def test_deploy_on_merge_dispatches_render_deploy_without_any_write_token() -> None:
    """#67: an armed merge is performed with GITHUB_TOKEN, whose push starts
    no workflow run, so `render-deploy`'s `push` trigger never fires for the
    machine lane. `deploy-on-merge` outlives the merge and fires
    `workflow_dispatch` — a documented exception to that recursion guard —
    instead. It must stay the weakest job in the file: `actions: write` to
    press the button and `pull-requests: read` to see the merge, never
    `contents: write` (it must not be able to merge anything itself), and no
    `uses:`/secret at all, same posture as `arm-auto-merge`."""
    text = _GOVERNANCE.read_text(encoding="utf-8")
    job = _job_block(text, "deploy-on-merge")
    assert _grant(job, "actions: write")
    assert not _grant(job, "contents: write")
    assert not _uses_refs(job)
    assert "secrets." not in job
    assert _runs(job, r"workflow run render-deploy\.yml .*--ref main")
    # Only ever for a PR the ownership-checked gate cleared for the machine
    # lane — a human-lane PR merges under a human identity, which fires
    # `push` on its own.
    assert re.search(r"(?m)^\s+if:.*disposition == 'success'", job)


def test_deploy_on_merge_never_shares_arm_auto_merges_concurrency_group() -> None:
    """This job waits minutes; `arm-auto-merge` must not queue behind it. A
    shared group would park a withdrawal for a newly non-machine-lane head
    behind a poll for the previous one — the exact stale-arm window
    `arm-auto-merge`'s own serialization exists to close. Its own group, and
    `cancel-in-progress` so a `synchronize` supersedes the poll rather than
    stacking a second one."""
    job = _job_block(_GOVERNANCE.read_text(encoding="utf-8"), "deploy-on-merge")
    assert re.search(r"(?m)^\s+group: deploy-on-merge-", job)
    assert re.search(r"(?m)^\s+cancel-in-progress: true", job)


# --- ND-4 reserved-brand carve-out (fork provenance) -----------------------


def test_reserved_namespace_flag_is_gated_on_a_same_repo_pull_request() -> None:
    """`--allow-reserved-namespace` unlocks ND-4's brand segments (`ocx`,
    `ocx-sh`, `ocx-contrib`, `ocx-rs`), which the operator's own
    `p/ocx/cli.json` and `p/ocx/mirror.json` roots need. Handing it to every
    `pull_request` run would let ANY fork PR claim `p/ocx/**` — publishing
    under the index's own brand — so it is passed only when the PR's head
    repository IS this repository. Asserted on the raw YAML: the flag must
    never appear in a `run:` line outside the guarded array, and the guard
    must be the provenance comparison, not e.g. an author-login test."""
    text = _VALIDATE.read_text(encoding="utf-8")
    job = _job_block(text, "schema-validate-pr")

    assert (
        "SAME_REPO_PR: ${{ github.event.pull_request.head.repo.full_name"
        " == github.repository }}" in job
    )
    assert re.search(r'(?m)^\s+if \[ "\$SAME_REPO_PR" = "true" \]; then$', job)
    # The flag reaches the CLI only through the array the guard fills.
    flag_lines = [
        line
        for line in job.splitlines()
        if "--allow-reserved-namespace" in line and not line.lstrip().startswith("#")
    ]
    assert flag_lines == ["            reserved+=(--allow-reserved-namespace)"]
    assert 'indexbot validate --base-dir "$BASE_DIR" "${reserved[@]}" "${files[@]}"' in job


def test_base_ref_bytes_are_materialized_outside_the_workspace() -> None:
    """ND-4 gates CLAIMING, not UPDATING, and `indexbot validate` can only
    tell those apart from the base-ref bytes of each changed root. They are
    written under `$RUNNER_TEMP`, never into the checkout: the PR-head tree is
    what `validate` byte-compares against its own canonical serialization, and
    a base-ref copy landing inside it would be validated as if it were part of
    the PR. A root absent at the base ref must be left unwritten, so `validate`
    sees nothing and falls back to "new claim"."""
    job = _job_block(_VALIDATE.read_text(encoding="utf-8"), "schema-validate-pr")
    assert 'base_dir="$RUNNER_TEMP/indexbot-base"' in job
    assert 'git cat-file -e "$BASE_SHA:$file" 2>/dev/null || continue' in job
    assert 'git show "$BASE_SHA:$file" > "$base_dir/$file"' in job
    assert "BASE_DIR: ${{ steps.base.outputs.dir }}" in job


def test_reserved_namespace_gate_lives_in_the_zero_secret_job() -> None:
    """The gate decides how PR-head content is *validated*, so it belongs in
    the unprivileged `pull_request` workflow — never in `governance.yml`,
    whose entire safety argument is that it runs no PR-head-derived logic."""
    for workflow in _workflow_files():
        if workflow == _VALIDATE:
            continue
        assert "--allow-reserved-namespace" not in workflow.read_text(encoding="utf-8")
