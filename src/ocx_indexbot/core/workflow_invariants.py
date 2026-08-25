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
from itertools import pairwise

from ocx_indexbot.core.policy import resolves_at_runtime

# A trailing comment is the common spelling ("permissions: {}  # jobs
# elevate individually") and must not read as a missing default-deny.
_PERMISSIONS_DEFAULT_DENY_RE = re.compile(r"(?m)^permissions:\s*\{\}\s*(?:#.*)?$")
_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TRIGGER_RE = re.compile(r"^  ([a-z_]+):")
# A job header may carry a trailing comment ("  arm-auto-merge:  # arms the
# merge"). Anchoring straight on end-of-line reads that line as ordinary
# content, and every job-scoped rule below then skips the job outright — a
# silent false clean, from a comment.
_JOB_RE = re.compile(r"^  ([A-Za-z0-9_-]+):[ \t]*(?:#.*)?$")
# A job header is never a comment, so a comment indented to the header column
# must not end the preceding job's block.
_NEXT_JOB_RE = re.compile(r"^  [^#\s]")
_STEPS_KEY_RE = re.compile(r"(?m)^[ ]+steps:[ \t]*(?:#.*)?$")
_FIRST_BULLET_RE = re.compile(r"(?m)^([ ]*)- ")
_JOB_IF_RE = re.compile(r"(?m)^\s{4}if:[ \t]*(.+)$")
_REF_KEY_RE = re.compile(r"(?m)^\s*ref:\s")
_PR_EVENT_NAME_IF_RE = re.compile(
    r"(?m)^\s+if:.*github\.event_name\s*[!=]=\s*'(?:pull_request|pull_request_target)'"
)
_CONTENTS_WRITE_RE = re.compile(r"(?m)^\s{6}contents: write\s*(?:#.*)?$")
_NEEDS_RE = re.compile(r"(?m)^\s{4}needs:\s*(.+)$")
_PERSIST_CREDENTIALS_FALSE_RE = re.compile(r"(?m)^\s*persist-credentials:\s*false\s*(?:#.*)?$")
# `${{ github.token }}` / `${{ secrets.ANYTHING }}`, in either case — GitHub
# resolves context names case-insensitively, so a matcher that does not would
# be bypassed by shouting.
_CREDENTIAL_RE = re.compile(r"\$\{\{[^}]*(?:github\.token|secrets\.[A-Za-z_])", re.IGNORECASE)


def _is_checkout(ref: str) -> bool:
    """Whether a `uses:` ref names a checkout action.

    Matched on the action's path rather than its owner, so a fork or a
    vendored re-publish of `actions/checkout` is still recognised — the
    credential-persisting behaviour travels with the action, not with who
    hosts it.
    """
    return ref.partition("@")[0].rstrip("/").rsplit("/", 1)[-1] == "checkout"


_PR_TRIGGERS = frozenset({"pull_request", "pull_request_target"})
# The triggers that hand a job base-repository credentials on an event an
# outside contributor can cause. `pull_request_target` is the famous one;
# `workflow_run` fires with the base repo's token regardless of who authored
# the run it reports on, and that run's `head_sha` is the fork's commit; and
# `issue_comment` fires for anybody who can type in a comment box. Naming only
# the first is how a `ref:` audit passes a workflow that checks out a fork's
# code and runs it with a write-scoped token.
#
# `workflow_call` is deliberately absent: a reusable workflow has no trigger of
# its own, so its privilege is whatever its caller's is, and the caller is
# where this audit can see it.
_PRIVILEGED_TRIGGERS = frozenset({"pull_request_target", "workflow_run", "issue_comment"})


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
    start = next(i for i, line in enumerate(lines) if (m := _JOB_RE.match(line)) and m[1] == job)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _NEXT_JOB_RE.match(lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def job_steps(block: str) -> list[str]:
    """One job's steps, each from its `- ` bullet to the next at that column.

    Step-scoped, because `with:` belongs to exactly one step. WF-06's
    `persist-credentials: false` has to be read off the checkout that carries
    it: searched job-wide, one hardened checkout vouches for every other
    checkout in the job, including the one taking the default `true`.

    The bullet column is taken from the first item rather than assumed to be
    six spaces, because YAML lets a sequence sit at its own key's indentation
    (`    steps:` then `    - uses: ...`) and GitHub runs that happily. A scan
    keyed on a fixed column finds no steps at all in such a job, and "no steps"
    reads as "nothing to flag" — a false clean produced by formatting. Taking
    the column from the first item is also what keeps a nested list inside a
    step's `with:` from reading as a step of its own.
    """
    key = _STEPS_KEY_RE.search(block)
    if key is None:
        return []
    tail = block[key.end() :]
    first = _FIRST_BULLET_RE.search(tail)
    if first is None:
        return []
    bullet = re.compile(rf"(?m)^{first[1]}- ")
    bounds = [match.start() for match in bullet.finditer(tail)]
    return [tail[start:end] for start, end in pairwise([*bounds, len(tail)])]


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
    `github.event.pull_request.head`. So under a privileged trigger — where the
    job holds real credentials — the absence of `ref:` anywhere in the file is
    the property to assert, not the absence of one particular expression.

    `pull_request_target` is not the only trigger with that property, and
    naming only it was a live false clean: a `workflow_run` job runs with
    base-repo credentials on a run a fork PR just caused, so
    `ref: ${{ github.event.workflow_run.head_sha }}` checks out that fork's
    commit and executes it with the base repository's token, and an
    `issue_comment` handler reaches the same place through
    `refs/pull/<n>/head`. Same breach, different event name — see
    `_PRIVILEGED_TRIGGERS`.
    """
    if not (events & _PRIVILEGED_TRIGGERS) or not _REF_KEY_RE.search(text):
        return []
    return [
        Finding(
            name,
            "WF-05",
            "a step sets `ref:` in a privileged workflow "
            "(`pull_request_target`/`workflow_run`/`issue_comment`) — the "
            "credentialed job must never check out contributor-controlled content",
        )
    ]


def _privileged_write_jobs(text: str, events: frozenset[str]) -> list[tuple[str, str]]:
    """Every job holding `contents: write` under `pull_request_target`, paired
    with its block.

    The one job shape in an index repository that can move the base branch on
    an event a stranger's pull request triggers, and therefore the subject of
    both WF-06 and WF-08 — which ask different questions about it: what a
    checkout leaves lying around, and what code the job resolves to run.

    Keyed on `_PRIVILEGED_TRIGGERS`, not on `pull_request_target` alone:
    `workflow_run` and `issue_comment` carry base-repo privileges the same way,
    and a job under either that holds `contents: write` is the same job shape
    wearing a different trigger.
    """
    if not (events & _PRIVILEGED_TRIGGERS):
        return []
    jobs: list[tuple[str, str]] = []
    for job in job_names(text):
        block = job_block(text, job)
        if _CONTENTS_WRITE_RE.search(block):
            jobs.append((job, block))
    return jobs


def _check_write_jobs_do_not_persist_credentials(
    name: str, text: str, events: frozenset[str]
) -> list[Finding]:
    """A job holding `contents: write` under a privileged trigger must keep its
    token in the step that spends it deliberately — out of `.git/config`, and
    out of another action's inputs.

    **This rule used to say something stronger, and it was retired on
    purpose.** It required such a job to run no `uses:` step of any kind — no
    checkout, no composite action — on the argument that a deferred write to
    the base branch is safe only while the job holding it executes nothing.
    That held while arming auto-merge was a `gh` one-liner. It stopped being
    expressible the moment arming became `indexbot governance-gate
    --arm-only`, because running this bot needs a checkout and a setup step,
    and both are `uses:`.

    What was actually given up: a job that can move the base branch now
    executes base-authored code and this package's own dependency tree. What
    was *not* given up is the part that matters — the code it runs is never
    pull-request-controlled (WF-05 forbids a `ref:` anywhere in a
    `pull_request_target` file, which is strictly stronger than a per-job
    rule), and which *version* of it runs is decided by a reviewed commit
    (WF-08). WF-02 is **not** what carries that second half, and this docstring
    used to claim it was: `_check_sha_pins` inspects `uses:` refs and says
    nothing about how a `run:` command resolves a package. WF-08 exists
    because that gap was load-bearing. The residual exposure is supply chain,
    and it is exposure the deployment already carries: the same pinned code
    runs in the job that renders and publishes the served index, which is what
    clients actually consume — a token that can move a branch protected
    against it is the smaller of those two.

    So the invariant narrows to the two ways such a job can leak the token it
    was granted, and stops asserting anything about *running* code.

    **The checkout half.** A checkout taking the default
    `persist-credentials: true` writes a `contents: write` token into
    `.git/config`, where every later step inherits it through plain `git` —
    dependency resolution and build backends included — with no
    `GITHUB_TOKEN` in sight to audit. The opt-out is read off each checkout
    step's own `with:` (`job_steps`): searched job-wide, the first hardened
    checkout would vouch for a second one taking the default under a different
    `path:`, whose `.git/config` holds the very same token.

    **The hand-off half**, which is the part the retired rule was actually
    protecting and which `persist-credentials: false` never reached: the job
    passes `${{ github.token }}` or a `${{ secrets.* }}` straight into a
    `uses:` step's inputs or environment. No checkout is needed to do it, so
    the checkout half cannot see it. Forwarding is refused outright rather
    than allowlisted per action, because the actions such a job legitimately
    runs are the *deployment's* (a local `./.github/actions/setup-bot`, here);
    a deployment-independent allowlist could only ever be a name, and a name
    is what a compromised release still has. What is deployment-independent is
    that a job which can move the base branch has no business handing that
    power to a step whose code it does not control — the token belongs in the
    `run:` step that spends it, where a reviewer reading the file sees it.

    Known blind spot: a credential that reaches an action indirectly — a job-
    level `env:` the action reads, or `${{ steps.app.outputs.token }}` minted
    by a GitHub App step — is not matched. Both are visible in review; what
    this catches is the unremarkable-looking `with: {token: ...}` line.
    """
    findings: list[Finding] = []
    for job, block in _privileged_write_jobs(text, events):
        for step in job_steps(block):
            refs = uses_refs(step)
            if any(_is_checkout(ref) for ref in refs):
                if not _PERSIST_CREDENTIALS_FALSE_RE.search(step):
                    findings.append(
                        Finding(
                            name,
                            "WF-06",
                            f"job `{job}` holds `contents: write` and checks out without "
                            "`persist-credentials: false` — the token would land in "
                            "`.git/config` for every later step to inherit",
                        )
                    )
            elif refs and _CREDENTIAL_RE.search(step):
                findings.append(
                    Finding(
                        name,
                        "WF-06",
                        f"job `{job}` holds `contents: write` and hands a credential to "
                        f"`uses: {refs[0]}` — a job that can move the base branch must "
                        "spend its token in its own `run:` step, never forward it",
                    )
                )
    return findings


def _check_write_jobs_run_a_pinned_bot(
    name: str, text: str, events: frozenset[str]
) -> list[Finding]:
    """A job holding `contents: write` under `pull_request_target` must not
    decide at job start which version of the bot it runs.

    The hole this closes was invisible to every other rule here. An operator
    commits the minimal policy this package documents, `indexbot ci` renders
    `arm-auto-merge` around a `uvx ocx-indexbot governance-gate --arm-only`,
    and that job holds a token that can move an unprotected base branch and
    squash-merge a pull request. `uvx` resolves the latest release when the
    step starts — no version, no lockfile, no hash — so one malicious release
    executes there. WF-02 does not cover it and never did: it inspects `uses:`
    refs, and this is a `run:` command. WF-06's narrowing rested on the code
    being "SHA-pinned (WF-02)", which was true of the actions and false of the
    bot; this rule is what makes that argument sound.

    Scoped to the credentialed job rather than every job, deliberately. The
    unprivileged `pull_request` lane resolves the bot at job start too, and
    that is fine — it holds no token, so a compromised release there reaches
    nothing a fork's own runner did not already have.

    What it cannot see: a composite action the job `uses:`, whose own steps
    are in `actions/**` and never audited here (`cli/workflows_check.py`
    reads the top level of the workflow directory and nothing below it). A
    deployment that hides its resolver in `ci.setup` passes this check and
    should not expect to.
    """
    findings: list[Finding] = []
    for job, block in _privileged_write_jobs(text, events):
        for line in block.splitlines():
            # Whole-line comments only: the argument for a rule is written out
            # beside it in these files, and `# a `uv` resolution failure` is
            # prose, not a step. A trailing comment on a real command line
            # stays in scope, where it cannot hide an invocation either.
            if line.lstrip().startswith("#") or not resolves_at_runtime(line):
                continue
            findings.append(
                Finding(
                    name,
                    "WF-08",
                    f"job `{job}` holds `contents: write` and resolves what it runs at "
                    f"job start — {line.strip()!r}. Pin the version by lockfile "
                    "(`--frozen`/`--locked`) or by exact specifier, so a reviewed commit "
                    "decides what executes with a token that can merge a pull request",
                )
            )
            break
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


def _job_if(block: str) -> str:
    """A job's own `if:` expression, trailing comment stripped.

    Read off the four-space `if:` key rather than searched for in the job
    block, because two very ordinary edits satisfy a substring search while
    leaving the job running on a fork: a *step*-level `if:`, which skips one
    step and no more, and a guard deleted but kept as a comment ("# guard used
    to be: ..."). Both were live false cleans in WF-07.

    Two known blind spots. A folded guard (`if: >-` continued on the next
    line) reads as empty and is flagged — the safe direction, a false finding
    rather than a false clean, and the fix is to write the guard on one line,
    which every template in this package does. And the caller matches the
    guard as a substring of this expression, so a *negated* one
    (`if: ${{ !(github.repository_owner == '...') }}`) passes; telling that
    apart needs an expression parser, and it is a deliberate act rather than
    the kind of edit that happens by accident.
    """
    match = _JOB_IF_RE.search(block)
    return match[1].partition(" #")[0].strip() if match else ""


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
        condition = _job_if(block)
        own = guard in condition or "github.event_name != 'schedule'" in condition
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
        findings.extend(_check_write_jobs_do_not_persist_credentials(name, text, events))
        findings.extend(_check_write_jobs_run_a_pinned_bot(name, text, events))
        if owner is not None:
            findings.extend(_check_cron_is_upstream_only(name, text, events, owner))
    return tuple(findings)
