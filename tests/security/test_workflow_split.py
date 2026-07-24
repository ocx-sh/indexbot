"""Static-assertion workflow-security suite (spec X7, register §5).

Covers G-14 (`permissions:` default-deny + SHA-pinned `uses:` across every
workflow) and G-16/FP-7 (the privileged `pull_request_target` governance job
never checks out PR head). The repo has no runtime YAML dependency and the
credential process must gain none, so these tests hand-parse the specific
keys (`permissions:`, `uses:`, `ref:`) with the stdlib only — a line scan,
never a YAML library import.
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
