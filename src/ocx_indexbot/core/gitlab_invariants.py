# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""Static invariants for a hand-written GitLab CI pipeline.

`indexbot ci --check` already gates `.gitlab-ci/indexbot.yml` byte-for-byte
against the policy it renders from, so a property of *that* file (its
schedules are upstream-guarded, it sets `GIT_STRATEGY: none`/`GIT_DEPTH: 0`
where it must) is the render-check's job, not this one's. What has no gate at
all is the root `.gitlab-ci.yml` an operator writes by hand and the files it
includes — the GitLab analogue of a hand-written GitHub workflow, which
`workflow_invariants.py` already audits. This module is that audit's GitLab
half.

Two rules only, deliberately: the two properties of hand-written GitLab YAML
that have no other gate and that the generated file gets right by
construction. Parsed the same way as `workflow_invariants.py` and for the
same reason — stdlib line scans keyed on indentation, no YAML library, so the
credentialed governance path gains no runtime dependency it does not already
have.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ocx_indexbot.core.workflow_invariants import Finding

# Top-level GitLab keywords that are never job names. A job or hidden
# template (`.foo:`) is any other top-level (zero-indent) mapping key.
#
# Deliberately does NOT list `pages`. GitLab spells both a special deploy JOB
# and (since 17.x) a config block that way, and the two are not separable by
# their key. Treating it as never-a-job would exempt a real `pages:` job from
# GL-03 — the e2e index's own pipeline has one — while treating it as a job
# costs nothing when it is the config block, which carries no `rules:` this
# rule reacts to. The safe error is the one that examines too much.
_RESERVED_TOP_LEVEL = frozenset(
    {
        "default",
        "variables",
        "include",
        "stages",
        "workflow",
        "image",
        "services",
        "before_script",
        "after_script",
        "cache",
        "spec",
    }
)

_JOB_RE = re.compile(r"^([A-Za-z0-9_.-]+):[ \t]*(?:#.*)?$")
_NEXT_JOB_RE = re.compile(r"^[^#\s]")
_IMAGE_LINE_RE = re.compile(r"^([ \t]*)image:[ \t]*(.*)$")
_NAME_LINE_RE = re.compile(r"^[ \t]*name:[ \t]*(\S+)")
_DIGEST_PIN_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
_MR_EVENT_RE = re.compile(r"\$CI_PIPELINE_SOURCE\s*==\s*[\"']merge_request_event[\"']")
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_CREDENTIAL_NAME_RE = re.compile(r"(?i)TOKEN|SECRET|PASSWORD|CREDENTIAL")
# GitLab's own per-job token, scoped to the running project — not a secret an
# operator configured, and the one credential every merge-request pipeline
# already legitimately holds. Flagging it would make every generated job here
# a finding, for a variable that carries none of the risk GL-03 is about.
_EXEMPT_VARS = frozenset({"CI_JOB_TOKEN"})
# Where a credential could plausibly be spent or interpolated. Deliberately
# not `rules:` — that section holds the trigger condition this rule already
# reads on its own, never a token, and searching it too would only risk a
# false match against `$CI_MERGE_REQUEST_PROJECT_PATH`-shaped comparisons.
_CREDENTIAL_SECTION_KEYS = ("script", "before_script", "variables")


def _strip_comment(value: str) -> str:
    return value.partition(" #")[0].strip()


def job_names(text: str) -> list[str]:
    """Every top-level mapping key that is not a reserved GitLab keyword —
    a job, or a hidden `.template:` only ever reached through `extends:`."""
    return [
        m[1]
        for line in text.splitlines()
        if (m := _JOB_RE.match(line)) and m[1] not in _RESERVED_TOP_LEVEL
    ]


def job_block(text: str, job: str) -> str:
    """One job's YAML text — from its zero-indent header to the next
    top-level key, or EOF."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if (m := _JOB_RE.match(line)) and m[1] == job)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _NEXT_JOB_RE.match(lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _section_text(block: str, key: str) -> str:
    """One `key:` section of a job block — from its own indentation to the
    next line at the same or shallower indentation. Blank lines never end a
    section; GitLab-authored YAML uses them freely for readability."""
    lines = block.splitlines()
    pattern = re.compile(rf"^([ \t]*){re.escape(key)}:[ \t]*(?:#.*)?$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match is None:
            continue
        depth = len(match[1])
        end = len(lines)
        for later in range(index + 1, len(lines)):
            candidate = lines[later]
            if not candidate.strip():
                continue
            if len(candidate) - len(candidate.lstrip()) <= depth:
                end = later
                break
        return "\n".join(lines[index:end])
    return ""


def _image_values(text: str) -> list[str]:
    """Every `image:` value in the file, scalar or mapping form.

    `image: foo@sha256:...` is the scalar spelling; `image:\\n  name:
    foo@sha256:...` is GitLab's mapping form, used when `image:` also carries
    `entrypoint:` or `pull_policy:`. Both appear at the top level (`default:`
    block) and per-job, so this scans the whole file rather than one job's
    block. A mapping form with no `name:` yields the empty string, which
    `_DIGEST_PIN_RE` never matches — a missing image name is exactly as
    unpinned as a missing tag.
    """
    lines = text.splitlines()
    values: list[str] = []
    for lineno, line in enumerate(lines):
        match = _IMAGE_LINE_RE.match(line)
        if match is None:
            continue
        indent, rest = match.groups()
        rest = _strip_comment(rest)
        if rest:
            values.append(rest)
            continue
        depth = len(indent)
        value = ""
        for later in lines[lineno + 1 :]:
            if not later.strip():
                continue
            if len(later) - len(later.lstrip()) <= depth:
                break
            if name_match := _NAME_LINE_RE.match(later):
                value = _strip_comment(name_match[1])
                break
        values.append(value)
    return values


def _check_image_digest_pinned(name: str, text: str) -> list[Finding]:
    """A GitLab job's `image:` is the exact analogue of a GitHub `uses:` ref
    (WF-02): the code running in a credentialed job must not change without a
    diff. A mutable tag (`oven/bun:1-alpine`, `python:3.13`, or no tag at
    all) means it does.
    """
    return [
        Finding(
            name,
            "GL-01",
            f"`image: {value}` is not pinned to a digest — expected "
            "`<host>/<path>@sha256:<64-hex>`",
        )
        for value in _image_values(text)
        if not _DIGEST_PIN_RE.fullmatch(value)
    ]


def _check_no_token_on_merge_request_event(name: str, text: str) -> list[Finding]:
    """A fork merge-request pipeline runs in the fork — the same trust
    boundary GitHub's plain `pull_request` gives, but only while the parent's
    credentials stay *protected* CI/CD variables. An operator who leaves
    `INDEXBOT_TOKEN` unprotected hands it to that fork pipeline, and no diff
    shows it: the exposure is a project setting, not a line of YAML.

    What IS visible in the YAML is the shape that would matter if the
    variable were unprotected — a job that runs on `merge_request_event` and
    references a token-shaped variable in its `script:`, `before_script:` or
    `variables:`. Not `rules:`: that section holds the trigger condition this
    rule already reads on its own and never a credential, and searching it too
    would only invite a false match on a `$CI_MERGE_REQUEST_*`-shaped
    comparison. `$CI_JOB_TOKEN` is exempt: it is
    GitLab's own per-job token, scoped to the project the pipeline is running
    in — for a fork MR, the fork — so it carries none of this hazard.

    Read off each job's own block, not through `extends:`. A job whose
    `rules:` live entirely on an extended template is not caught; the
    generated templates never split `rules:` out that way; a hand-written
    pipeline that does should read the credential straight off the job it
    actually appears in.
    """
    findings: list[Finding] = []
    for job in job_names(text):
        block = job_block(text, job)
        if not _MR_EVENT_RE.search(_section_text(block, "rules")):
            continue
        found: set[str] = set()
        for key in _CREDENTIAL_SECTION_KEYS:
            for match in _VAR_REF_RE.finditer(_section_text(block, key)):
                var = match[1]
                if var not in _EXEMPT_VARS and _CREDENTIAL_NAME_RE.search(var):
                    found.add(var)
        for var in sorted(found):
            findings.append(
                Finding(
                    name,
                    "GL-03",
                    f"job `{job}` runs on `merge_request_event` and references "
                    f"`${var}` — a fork merge request runs this job in the fork, "
                    "so the variable must be a protected CI/CD variable or absent "
                    "from this job entirely",
                )
            )
    return findings


def check_gitlab(pipeline: Mapping[str, str]) -> tuple[Finding, ...]:
    """Every GitLab invariant, over every given file, sorted by file name.

    `pipeline` maps a display name (the root `.gitlab-ci.yml` and each
    included file's path) to its text.
    """
    findings: list[Finding] = []
    for name in sorted(pipeline):
        text = pipeline[name]
        findings.extend(_check_image_digest_pinned(name, text))
        findings.extend(_check_no_token_on_merge_request_event(name, text))
    return tuple(findings)
