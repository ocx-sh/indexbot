# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""Static workflow-security invariants for an index repository.

An index repo runs this bot under `pull_request_target` with a write-scoped
token beside a `pull_request` job that checks out untrusted PR head. That
arrangement is safe only while a handful of structural properties hold, and
every one of them is a property of *the repository's YAML*, not of this
package's code — which is why they are checked here rather than asserted in a
test that could only ever see this repo's own workflows.

The rules below are the deployment-independent half of what
`ocx-sh/index`'s security suite used to assert against its own tree: they name
no job, no step and no incident. A deployment's *particular* arrangement (the
job names, the `gh` invocations that arm auto-merge, the incident witnesses)
stays in that repository's own tests, where the evidence lives.

Parsed with the standard library only — line scans keyed on the exact mapping
indentation, never a YAML library. The credentialed governance path must gain
no runtime dependency it does not already have, and a parser is a dependency.
The cost is that these scans assume conventional 2-space GitHub Actions
formatting; `actionlint` in the same pipeline is what keeps that true.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# A trailing comment is the common spelling ("permissions: {}  # jobs
# elevate individually") and must not read as a missing default-deny.
_PERMISSIONS_DEFAULT_DENY_RE = re.compile(r"(?m)^permissions:\s*\{\}\s*(?:#.*)?$")
_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TRIGGER_RE = re.compile(r"^  ([a-z_]+):")
_JOB_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_REF_KEY_RE = re.compile(r"(?m)^\s*ref:\s")
_PR_EVENT_NAME_IF_RE = re.compile(
    r"(?m)^\s+if:.*github\.event_name\s*[!=]=\s*'(?:pull_request|pull_request_target)'"
)
_CONTENTS_WRITE_RE = re.compile(r"(?m)^\s{6}contents: write\s*(?:#.*)?$")
_NEEDS_RE = re.compile(r"(?m)^\s{4}needs:\s*(.+)$")

_PR_TRIGGERS = frozenset({"pull_request", "pull_request_target"})


@dataclass(frozen=True, slots=True)
class Finding:
    """One violated invariant, in one workflow file."""

    workflow: str
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.workflow}: [{self.rule}] {self.message}"


def triggers(text: str) -> frozenset[str]:
    """The event names in a workflow's `on:` block — the two-space mapping
    keys between `on:` and the next top-level key.

    Comment lines are indented but never match, because they start with `#`.
    A workflow with no `on:` block at all is not a workflow; it yields the
    empty set rather than raising, so one malformed file cannot abort the
    audit of every other.
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "on:"), None)
    if start is None:
        return frozenset()
    events: set[str] = set()
    for line in lines[start + 1 :]:
        if re.match(r"^\S", line):
            break
        if match := _TRIGGER_RE.match(line):
            events.add(match.group(1))
    return frozenset(events)


def uses_refs(text: str) -> list[str]:
    """Every `uses:` value in the given text, in file order."""
    return [m.group(1) for line in text.splitlines() if (m := _USES_RE.match(line))]


def job_names(text: str) -> list[str]:
    """Every job name — the two-space mapping keys after `jobs:`."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "jobs:"), None)
    if start is None:
        return []
    return [m.group(1) for line in lines[start + 1 :] if (m := _JOB_RE.match(line))]


def job_block(text: str, job: str) -> str:
    """One job's YAML text — from its two-space header to the next one, or EOF."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == f"  {job}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _check_default_deny(name: str, text: str) -> list[Finding]:
    if _PERMISSIONS_DEFAULT_DENY_RE.search(text):
        return []
    return [
        Finding(
            name,
            "WF-01",
            "no top-level `permissions: {}` — a workflow without an explicit "
            "default-deny inherits the repository default, which may be write",
        )
    ]


def _check_sha_pins(name: str, text: str) -> list[Finding]:
    """Local `./` composite actions and `docker://` refs are exempt: neither
    is a marketplace ref that a tag could be moved on."""
    findings: list[Finding] = []
    for ref in uses_refs(text):
        if ref.startswith(("./", "docker://")):
            continue
        _, _, pin = ref.partition("@")
        if not _SHA_RE.fullmatch(pin):
            findings.append(
                Finding(name, "WF-02", f"`uses: {ref}` is not pinned to a 40-hex commit SHA")
            )
    return findings


def _check_one_pr_trigger(name: str, events: frozenset[str]) -> list[Finding]:
    """`pull_request` and `pull_request_target` fire on the same head commit,
    so a workflow carrying both must discriminate them with a job-level
    `if: github.event_name == ...` — and a job skipped by such an `if:` STILL
    emits a check run, conclusion `skipped`, under its own name. GitHub counts
    `skipped` as satisfying a required status check and resolves duplicate
    context names to the most recent run, so the privileged half publishes a
    green-equivalent impostor of the unprivileged half's required context.
    """
    if not events >= _PR_TRIGGERS:
        return []
    return [
        Finding(
            name,
            "WF-03",
            "declares both `pull_request` and `pull_request_target` — the "
            "trigger-discriminating job `if:` this forces emits skipped check "
            "runs under the other half's required context name; split the file",
        )
    ]


def _check_no_event_name_if(name: str, text: str, events: frozenset[str]) -> list[Finding]:
    """The structural half of the same fix: with one trigger per file there is
    nothing for a *pull-request* `github.event_name` guard to decide, and
    reintroducing one is how the skipped-check-run collision comes back.

    Only comparisons against a PR event literal count. `github.event_name !=
    'schedule'` on a `push`/`pull_request`/`schedule` workflow is the ordinary
    way to keep a nightly run from re-running every PR gate, and has nothing to
    do with this hazard.
    """
    if not (events & _PR_TRIGGERS) or not _PR_EVENT_NAME_IF_RE.search(text):
        return []
    return [
        Finding(
            name,
            "WF-04",
            "a job `if:` discriminates on `github.event_name` in a "
            "pull-request workflow — see WF-03",
        )
    ]


def _check_no_head_checkout(name: str, text: str, events: frozenset[str]) -> list[Finding]:
    """A checkout with no `ref:` defaults to the base branch tip; the only way
    to check out PR head is an explicit `ref:` resolving
    `github.event.pull_request.head`. So under `pull_request_target` — where
    the job holds real credentials — the absence of `ref:` anywhere in the file
    is the property to assert, not the absence of one particular expression.
    """
    if "pull_request_target" not in events or not _REF_KEY_RE.search(text):
        return []
    return [
        Finding(
            name,
            "WF-05",
            "a step sets `ref:` in a `pull_request_target` workflow — the "
            "credentialed job must never check out PR-head content",
        )
    ]


def _check_write_jobs_run_no_code(name: str, text: str, events: frozenset[str]) -> list[Finding]:
    """A job holding `contents: write` under `pull_request_target` must run no
    repository code at all: no `uses:` of any kind, so no checkout and no
    composite action that could forward its token. Deferred writes to the base
    branch (arming auto-merge, dispatching a deploy) are safe only while the
    job holding them executes nothing.

    `contents: write` specifically, not any write scope. A gate job legitimately
    holds `pull-requests`/`statuses`/`issues: write` to label, comment and
    publish a check while running this bot's own source; what it must never
    hold is the scope that can move the base branch.
    """
    if "pull_request_target" not in events:
        return []
    findings: list[Finding] = []
    for job in job_names(text):
        block = job_block(text, job)
        if _CONTENTS_WRITE_RE.search(block) and uses_refs(block):
            findings.append(
                Finding(
                    name,
                    "WF-06",
                    f"job `{job}` holds `contents: write` and also runs `uses:` "
                    "steps — a write-scoped job under `pull_request_target` must "
                    "execute no repository code",
                )
            )
    return findings


def _job_needs(block: str) -> list[str]:
    """A job's `needs:` dependencies, from either the scalar or the inline-list
    form (`needs: guard` / `needs: [guard, build]`). The block form
    (`needs:\n      - guard`) is not used in this family and is not parsed."""
    match = _NEEDS_RE.search(block)
    if match is None:
        return []
    value = match.group(1).strip()
    if value.startswith("["):
        value = value.strip("[]")
    return [dep.strip().strip("\"'") for dep in value.split(",") if dep.strip()]


def _check_cron_is_upstream_only(
    name: str, text: str, events: frozenset[str], owner: str
) -> list[Finding]:
    """Every fork inherits every `schedule:` and runs it off its own stale YAML.
    A job a schedule can reach therefore carries an owner guard, or excludes the
    schedule event outright — or `needs:` a job that does, which is how a
    workflow guards a whole graph at one entry point instead of repeating the
    expression on every job. The inheritance is transitive: a job needing a
    guarded job is itself unreachable on a fork, because a skipped dependency
    skips its dependents.
    """
    if "schedule" not in events:
        return []
    guard = f"github.repository_owner == '{owner}'"
    blocks = {job: job_block(text, job) for job in job_names(text)}
    guarded: dict[str, bool] = {}

    def is_guarded(job: str, seen: frozenset[str]) -> bool:
        """`seen` breaks a `needs:` cycle — invalid YAML that GitHub itself
        rejects, but this must terminate rather than trust the input."""
        if job in guarded:
            return guarded[job]
        block = blocks.get(job)
        if block is None or job in seen:
            return False
        own = guard in block or "github.event_name != 'schedule'" in block
        verdict = own or any(is_guarded(dep, seen | {job}) for dep in _job_needs(block))
        guarded[job] = verdict
        return verdict

    return [
        Finding(
            name,
            "WF-07",
            f"job `{job}` is reachable by `schedule:` without an upstream "
            f"guard — add `if: {guard}`, exclude the schedule event, or "
            "depend on a job that does",
        )
        for job in blocks
        if not is_guarded(job, frozenset())
    ]


def check_workflows(workflows: Mapping[str, str], *, owner: str | None) -> tuple[Finding, ...]:
    """Every invariant, over every given workflow, sorted by file name.

    `workflows` maps a display name (the file's basename) to its text.
    `owner` enables the cron guard check (WF-07); `None` skips it, for a
    caller that cannot name the upstream owner.
    """
    findings: list[Finding] = []
    for name in sorted(workflows):
        text = workflows[name]
        events = triggers(text)
        findings.extend(_check_default_deny(name, text))
        findings.extend(_check_sha_pins(name, text))
        findings.extend(_check_one_pr_trigger(name, events))
        findings.extend(_check_no_event_name_if(name, text, events))
        findings.extend(_check_no_head_checkout(name, text, events))
        findings.extend(_check_write_jobs_run_no_code(name, text, events))
        if owner is not None:
            findings.extend(_check_cron_is_upstream_only(name, text, events, owner))
    return tuple(findings)
