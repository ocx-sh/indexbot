"""Static-assertion workflow-security suite (spec X7, register §5).

Covers G-14 (`permissions:` default-deny + SHA-pinned `uses:` across every
workflow), G-16/FP-7 (the privileged `pull_request_target` governance job
never checks out PR head), and the `contents: write` split that lets
`validate.yml` arm auto-merge at all: the write token lives in a job that
checks nothing out, and never in the job that runs `bot/`'s source. The repo
has no runtime YAML dependency and the credential process must gain none, so
these tests hand-parse the specific keys (`permissions:`, `uses:`, `ref:`)
with the stdlib only — a line scan, never a YAML library import.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

_PERMISSIONS_DEFAULT_DENY_RE = re.compile(r"(?m)^permissions:\s*\{\}\s*$")
_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _workflow_files() -> list[Path]:
    return sorted(_WORKFLOWS_DIR.glob("*.yml"))


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


# --- G-16 / FP-7 -----------------------------------------------------------


def test_pull_request_target_governance_job_never_checks_out_pr_head() -> None:
    """G-16/FP-7: `validate.yml`'s privileged `governance-gate` job runs under
    `pull_request_target`, checks out the base ref only (no `ref:` key), never
    resolves `github.event.pull_request.head`, and holds no PAT secret — the
    untrusted PR-head content never runs in the credentialed job."""
    text = (_WORKFLOWS_DIR / "validate.yml").read_text(encoding="utf-8")
    privileged = _job_block(text, "governance-gate")
    assert "github.event_name == 'pull_request_target'" in privileged
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
    text = (_WORKFLOWS_DIR / "validate.yml").read_text(encoding="utf-8")
    unprivileged = _job_block(text, "schema-validate-pr")
    assert "github.event_name == 'pull_request'" in unprivileged
    assert "github.event.pull_request.head.sha" in unprivileged
    assert "secrets." not in unprivileged


# --- contents: write split -------------------------------------------------


def test_governance_gate_never_holds_contents_write() -> None:
    """`governance-gate` checks out and runs `bot/`'s source and forwards
    `github.token` to a third-party action via `setup-bot`. It therefore
    holds `contents: read` and must never be "simplified" into holding the
    write token that arming auto-merge needs."""
    text = (_WORKFLOWS_DIR / "validate.yml").read_text(encoding="utf-8")
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
    text = (_WORKFLOWS_DIR / "validate.yml").read_text(encoding="utf-8")
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
    text = (_WORKFLOWS_DIR / "validate.yml").read_text(encoding="utf-8")
    arm = _job_block(text, "arm-auto-merge")
    assert _runs(arm, r"pr view .*--json autoMergeRequest")
    assert not re.search(r"(?m)^(?!\s*#).*\bgh pr merge[^\n]*--disable-auto[^\n]*\|\|", arm)
    assert re.search(r"(?m)^\s+if:.*!cancelled\(\)", arm)
    assert re.search(r"(?m)^\s+if:.*github\.event_name == 'pull_request_target'", arm)
    # Serialized per PR: an arm job from an older head must never execute
    # after the withdrawal triggered by a newer one.
    assert re.search(r"(?m)^\s+group: arm-auto-merge-", arm)
