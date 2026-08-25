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

# A job header may carry a trailing anchor (`deploy: &deploy`, so `extends:`-
# style YAML merges can reference it) and/or a trailing comment, in either
# order relative to each other is not something GitLab supports — the anchor
# always comes right after the colon. Without the anchor group, `_JOB_RE`
# never matched such a header at all, and every rule keyed on `job_names`
# (GL-03 in particular) was blind to the job entirely — not a false clean on
# one finding, an invisible job.
_JOB_RE = re.compile(r"^([A-Za-z0-9_.-]+):[ \t]*(?:&[A-Za-z0-9_-]+[ \t]*)?(?:#.*)?$")
_NEXT_JOB_RE = re.compile(r"^[^#\s]")
_IMAGE_LINE_RE = re.compile(r"^([ \t]*)image:[ \t]*(.*)$")
_NAME_LINE_RE = re.compile(r"^[ \t]*name:[ \t]*(\S+)")
_DIGEST_PIN_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
# A block-scalar header (`script: |`, `before_script: >-2`) — everything
# indented deeper than the key is the scalar's own text, not further YAML.
# `_image_values` needs this so a shell body that happens to `echo "image:
# python:3.13"` is never read as a real `image:` key.
_BLOCK_SCALAR_RE = re.compile(r"^([ \t]*)[A-Za-z0-9_.-]+:[ \t]*[|>][+-]?[0-9]?[ \t]*(?:#.*)?$")
_MR_EVENT_RE = re.compile(r"\$CI_PIPELINE_SOURCE\s*==\s*[\"']merge_request_event[\"']")
# GitLab's pre-`rules:` trigger syntax. `only:` is a list, so both the block
# form (`only:\n  - merge_requests`) and the inline form (`only:
# [merge_requests]`) put the keyword somewhere `_section_text` already
# returns — see its own note on why the key's line is part of the section.
# Word-bounded so an unrelated ref name that merely contains the keyword as a
# substring (there is no such GitLab-meaningful ref, but nothing stops an
# operator naming a branch that way) is not read as the trigger.
_ONLY_MERGE_REQUESTS_RE = re.compile(r"\bmerge_requests\b")
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# TOKEN/SECRET/PASSWORD/CREDENTIAL is the original set. Widened here because
# a hand-written pipeline audited under GL-03 named its credential $GLPAT,
# $DEPLOY_KEY and $NPM_AUTH at least as often as $...TOKEN — none of those
# three matched before.
#
#   KEY, AUTH, PRIVATE — added as plain substrings. Each will also flag an
#   innocuous name that happens to contain it (an `$SSH_PUBLIC_KEY`, say) —
#   accepted, on the same principle `job_names` already states for `pages:`:
#   the safe error here is the one that examines too much, because a false
#   *finding* costs a reviewer one glance and a false *clean* costs nothing
#   at all until a fork pipeline reads a parent's token.
#
#   PAT — added with a `(?!H)` carve-out for the one collision that is not
#   hypothetical: `$CI_PROJECT_PATH` and `$CI_PROJECT_PATH_SLUG` are real,
#   commonly-logged GitLab predefined variables, not credentials, and bare
#   `PAT` would flag both on nearly every pipeline that prints its own
#   project path. `PAT(?!H)` still catches the reported `$GLPAT` shape; the
#   cost is missing a hypothetical `$...PATCH...`-named variable, which is
#   the direction this rule already prefers over flagging a GitLab builtin.
#
#   APIKEY was suggested and deliberately left out: it is `KEY` with a
#   prefix, already matched by the bare term above, and re-adding it as its
#   own alternative would not catch anything `KEY` does not already catch.
_CREDENTIAL_NAME_RE = re.compile(r"(?i)TOKEN|SECRET|PASSWORD|CREDENTIAL|KEY|AUTH|PRIVATE|PAT(?!H)")
# GitLab's own per-job token, scoped to the running project (not an operator
# secret), and its own commit-author string (a name/email, never a secret) —
# `CI_COMMIT_AUTHOR` is the one builtin the widened `AUTH` term newly
# reaches; a pass over the rest of GitLab's predefined-variable set found
# nothing else any of the new terms catch.
_EXEMPT_VARS = frozenset({"CI_JOB_TOKEN", "CI_COMMIT_AUTHOR"})
# Where a credential could plausibly be spent or interpolated. Deliberately
# not `rules:` — that section holds the trigger condition this rule already
# reads on its own, never a token, and searching it too would only risk a
# false match against `$CI_MERGE_REQUEST_PROJECT_PATH`-shaped comparisons.
_CREDENTIAL_SECTION_KEYS = ("script", "before_script", "after_script", "variables")


def _strip_comment(value: str) -> str:
    return value.partition(" #")[0].strip()


def _strip_quotes(value: str) -> str:
    """Strip one symmetric pair of surrounding quotes (`'...'` or `"..."`).

    A digest-pinned image is exactly as pinned quoted as bare
    (`image: "foo@sha256:..."` is ordinary YAML, not a different value), but
    `_DIGEST_PIN_RE` matches the bytes between the quotes, not the quotes
    themselves — GL-01 read a correctly pinned quoted image as unpinned
    before this, a false finding rather than a false clean, but adoption
    friction all the same.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def job_names(text: str) -> list[str]:
    """Every top-level mapping key that is not a reserved GitLab keyword —
    a job, or a hidden `.template:` only ever reached through `extends:`.
    A trailing YAML anchor on the header (`deploy: &deploy`) does not stop
    it from being recognised."""
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
    """One `key:` section of a job block — the key's own line, plus everything
    indented under it, up to the next line at the same or shallower
    indentation. Blank lines never end a section; GitLab-authored YAML uses
    them freely for readability.

    The key line is part of the section, and whatever follows the colon on it
    is not required to be empty. Both matter: `script: echo "$TOKEN"` puts the
    credential on the key line itself, and `script: |` puts a block-scalar
    indicator there — a pattern anchored on end-of-line matched neither, so
    the section came back empty and GL-03 read a job holding a token as
    holding none. That is a silent clean, which is the only failure mode a
    rule like this really has.

    Not anchored to column zero: this is the right shape for a section
    *inside* a job's own block, where `block` already starts at that job's
    header and cannot contain another job's same-named key first. Scanning a
    whole file for a top-level section needs `top_level_section` instead —
    unanchored here, a job's own nested `variables:` would shadow the file's
    real top-level one.
    """
    lines = block.splitlines()
    pattern = re.compile(rf"^([ \t]*){re.escape(key)}:")
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


def top_level_section(text: str, key: str) -> str:
    """One top-level (zero-indent) `key:` section of a whole pipeline file —
    the key's own line plus everything indented under it, up to the next
    zero-indent line.

    Anchored to column zero, unlike `_section_text`: called against the
    *whole file*, where a job may carry a same-named nested key of its own
    (a per-job `variables:` is the case that matters — GL-03's top-level
    credential scan must read the file's actual top-level block, not
    whichever job's `variables:` happens to appear first). Also used by
    `cli/workflows_check.py`'s `include:` loader, which needs the identical
    column-zero section extraction for a different key.
    """
    lines = text.splitlines()
    pattern = re.compile(rf"^{re.escape(key)}:")
    for index, line in enumerate(lines):
        if pattern.match(line) is None:
            continue
        end = len(lines)
        for later in range(index + 1, len(lines)):
            candidate = lines[later]
            # A blank line, or a comment sitting at column zero between two
            # top-level keys, is not itself a top-level key and must not cut
            # the section short — the same discipline `job_block`/
            # `_NEXT_JOB_RE` already applies one level down.
            if not candidate.strip() or candidate.startswith("#"):
                continue
            if candidate[0] not in " \t":
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

    A line inside a block scalar body (`script: |`, then indented shell
    text) is skipped outright, tracked by `block_scalar_depth`: without this,
    a script that echoes the literal text `image: python:3.13` — for a log
    line, a Dockerfile heredoc, anything — was read as a second, unpinned
    `image:` key that does not exist anywhere the pipeline actually runs.
    """
    lines = text.splitlines()
    values: list[str] = []
    block_scalar_depth: int | None = None
    for lineno, line in enumerate(lines):
        if block_scalar_depth is not None:
            if line.strip() and (len(line) - len(line.lstrip())) <= block_scalar_depth:
                block_scalar_depth = None
            else:
                continue
        if block_match := _BLOCK_SCALAR_RE.match(line):
            block_scalar_depth = len(block_match[1])
            continue
        match = _IMAGE_LINE_RE.match(line)
        if match is None:
            continue
        indent, rest = match.groups()
        rest = _strip_quotes(_strip_comment(rest))
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
                value = _strip_quotes(_strip_comment(name_match[1]))
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


def _job_triggers_on_merge_request(block: str, *, pipeline_wide: bool) -> bool:
    """Whether a job's own block runs on `merge_request_event`, by any shape
    GitLab accepts: a pipeline-wide `workflow: rules:` gate that applies
    regardless of what the job itself declares, the job's own `rules:`, or
    the job's legacy `only:`.

    There is no top-level equivalent of legacy `only:` the way `workflow:`
    is one for `rules:` — GitLab never gave the old syntax a pipeline-wide
    form — so `only:` is only ever read off the job's own block.
    """
    if pipeline_wide:
        return True
    if _MR_EVENT_RE.search(_section_text(block, "rules")):
        return True
    return bool(_ONLY_MERGE_REQUESTS_RE.search(_section_text(block, "only")))


def _credential_vars(section: str) -> set[str]:
    """Every credential-shaped `$VAR`/`${VAR}` reference in a section's text,
    `_EXEMPT_VARS` excluded."""
    return {
        match[1]
        for match in _VAR_REF_RE.finditer(section)
        if match[1] not in _EXEMPT_VARS and _CREDENTIAL_NAME_RE.search(match[1])
    }


def _check_no_token_on_merge_request_event(name: str, text: str) -> list[Finding]:
    """A fork merge-request pipeline runs in the fork — the same trust
    boundary GitHub's plain `pull_request` gives, but only while the parent's
    credentials stay *protected* CI/CD variables. An operator who leaves
    `INDEXBOT_TOKEN` unprotected hands it to that fork pipeline, and no diff
    shows it: the exposure is a project setting, not a line of YAML.

    What IS visible in the YAML is the shape that would matter if the
    variable were unprotected — a job that runs on `merge_request_event`
    (whether that comes from a pipeline-wide `workflow: rules:` gate, the
    job's own `rules:`, or the job's legacy `only:` — see
    `_job_triggers_on_merge_request`) and references a token-shaped variable
    in its `script:`, `before_script:`, `after_script:` or `variables:`, or
    inherits one from the file's top-level `variables:` block. Not `rules:`:
    that section holds the trigger condition this rule already reads on its
    own and never a credential, and searching it too would only invite a
    false match on a `$CI_MERGE_REQUEST_*`-shaped comparison. `$CI_JOB_TOKEN`
    is exempt: it is GitLab's own per-job token, scoped to the project the
    pipeline is running in — for a fork MR, the fork — so it carries none of
    this hazard.

    Read off each job's own block, not through `extends:`. A job inheriting
    both its `rules:` and its credential from a template is therefore not
    itself flagged — but the *template* is, because a hidden `.name:` key is
    a top-level mapping key like any other and `job_names` returns it. The
    finding lands on the line an operator has to edit anyway, which is why
    resolving `extends:` chains buys nothing here. What genuinely escapes is
    only the split case: `rules:` on the job and the credential on a template
    it extends, or the reverse.

    The identical split exists for `rules: !reference [.mr, rules]` — GitLab's
    custom tag for pulling another key's value in verbatim. Following it
    needs the same graph resolution `extends:` does, which this line-scan
    deliberately does not build, so a job written that way is invisible to
    this rule for the same reason and to the same degree as the `extends:`
    split above.
    """
    pipeline_wide = bool(_MR_EVENT_RE.search(top_level_section(text, "workflow")))
    findings: list[Finding] = []
    mr_jobs: list[str] = []
    for job in job_names(text):
        block = job_block(text, job)
        if not _job_triggers_on_merge_request(block, pipeline_wide=pipeline_wide):
            continue
        mr_jobs.append(job)
        found: set[str] = set()
        for key in _CREDENTIAL_SECTION_KEYS:
            found |= _credential_vars(_section_text(block, key))
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

    if mr_jobs:
        for var in sorted(_credential_vars(top_level_section(text, "variables"))):
            findings.append(
                Finding(
                    name,
                    "GL-03",
                    f"top-level `variables:` block references `${var}`, inherited by "
                    f"every merge-request-triggered job ({', '.join(mr_jobs)}) — a fork "
                    "merge request runs those jobs in the fork, so the variable must be "
                    "a protected CI/CD variable or removed from this file's top level",
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
