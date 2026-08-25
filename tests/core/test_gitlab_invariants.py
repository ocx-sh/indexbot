# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""One planted violation per GitLab invariant, plus the near-misses.

Same discipline as `test_workflow_invariants.py`: inline fixtures small enough
to read in one screen, a near-miss test for every predicate that could
plausibly over-fire, and a direct test of every parser helper rather than
only through `check_gitlab`.
"""

from __future__ import annotations

from ocx_indexbot.core.gitlab_invariants import (
    check_gitlab,
    job_block,
    job_names,
    top_level_section,
)

_DIGEST = "a" * 64

# Shaped like the real `ci/templates/gitlab/indexbot.yml`: a pinned
# `default.image`, a hidden template reached only through `extends:`, and one
# job whose own `rules:` (not the template's) reach `merge_request_event`.
_CLEAN = f"""\
default:
  image: python@sha256:{_DIGEST}

.indexbot:
  before_script:
    - indexbot --version

indexbot-validate:
  extends: .indexbot
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - indexbot validate-pr
"""


def _rules(text: str, *, name: str = "gitlab-ci.yml") -> list[str]:
    return [finding.rule for finding in check_gitlab({name: text})]


def test_a_clean_pipeline_has_no_findings() -> None:
    assert check_gitlab({".gitlab-ci.yml": _CLEAN}) == ()


def test_findings_are_sorted_by_file_name() -> None:
    broken = _CLEAN.replace(f"python@sha256:{_DIGEST}", "python:3.13")
    findings = check_gitlab({"z.yml": broken, "a.yml": broken})
    assert [finding.workflow for finding in findings] == ["a.yml", "z.yml"]


# --- GL-01: every `image:` is digest-pinned ---------------------------------


def test_gl01_flags_a_floating_tag() -> None:
    assert _rules(_CLEAN.replace(f"python@sha256:{_DIGEST}", "python:3.13")) == ["GL-01"]


def test_gl01_flags_an_image_with_no_tag_at_all() -> None:
    assert _rules(_CLEAN.replace(f"python@sha256:{_DIGEST}", "python")) == ["GL-01"]


def test_gl01_allows_a_trailing_comment_on_a_pinned_scalar() -> None:
    commented = _CLEAN.replace(
        f"image: python@sha256:{_DIGEST}",
        f"image: python@sha256:{_DIGEST}  # pinned 2026-08",
    )
    assert _rules(commented) == []


def test_gl01_scans_every_image_line_not_only_default() -> None:
    """A job may override `default.image`; the scan is file-wide, not
    job-scoped, so a floating per-job override is caught too."""
    text = f"""\
default:
  image: python@sha256:{_DIGEST}

indexbot-custom:
  image: oven/bun:1-alpine
  script:
    - echo hi
"""
    assert _rules(text) == ["GL-01"]


def test_gl01_reads_the_mapping_form() -> None:
    text = f"""\
default:
  image:
    name: python@sha256:{_DIGEST}
    entrypoint: [""]
"""
    assert check_gitlab({"x.yml": text}) == ()


def test_gl01_flags_an_unpinned_mapping_form() -> None:
    text = """\
default:
  image:
    name: python:3.13
    entrypoint: [""]
"""
    assert _rules(text) == ["GL-01"]


def test_gl01_flags_a_mapping_with_no_name_key_before_the_next_top_level_key() -> None:
    text = """\
default:
  image:
    pull_policy: always

indexbot-validate:
  script:
    - echo hi
"""
    assert _rules(text) == ["GL-01"]


def test_gl01_flags_a_mapping_with_no_name_key_that_runs_to_eof() -> None:
    """The `image:` mapping is the last thing in the file — the scan for a
    nested `name:` has nothing left to hit a lower indentation against."""
    text = """\
default:
  image:
    pull_policy: always
"""
    assert _rules(text) == ["GL-01"]


# --- GL-03: no token variable on a `merge_request_event` job ----------------


def test_gl03_flags_a_token_referenced_in_script() -> None:
    leaky = _CLEAN.replace(
        "    - indexbot validate-pr",
        '    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com',
    )
    assert _rules(leaky) == ["GL-03"]


def test_gl03_flags_a_braced_variable_reference() -> None:
    leaky = _CLEAN.replace(
        "    - indexbot validate-pr",
        '    - curl -H "PRIVATE-TOKEN: ${INDEXBOT_TOKEN}" https://example.com',
    )
    assert _rules(leaky) == ["GL-03"]


def test_gl03_flags_a_token_referenced_in_before_script() -> None:
    text = f"""\
default:
  image: python@sha256:{_DIGEST}

indexbot-leaky:
  before_script:
    - export SECRET_HEADER="$INDEXBOT_TOKEN"
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - echo hi
"""
    assert _rules(text) == ["GL-03"]


def test_gl03_flags_a_token_referenced_in_variables() -> None:
    text = f"""\
default:
  image: python@sha256:{_DIGEST}

indexbot-leaky:
  variables:
    HEADER: $DEPLOY_PASSWORD

  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - echo hi
"""
    assert _rules(text) == ["GL-03"]


def test_gl03_findings_are_sorted_by_variable_name() -> None:
    text = f"""\
default:
  image: python@sha256:{_DIGEST}

indexbot-leaky:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    A: $ZEBRA_SECRET
  script:
    - curl -H "PRIVATE-TOKEN: $ALPHA_TOKEN" https://example.com
"""
    first, second = check_gitlab({"x.yml": text})
    assert "$ALPHA_TOKEN" in first.message
    assert "$ZEBRA_SECRET" in second.message


def test_gl03_ci_job_token_is_exempt() -> None:
    """`$CI_JOB_TOKEN` is GitLab's own per-job token, scoped to the project
    the pipeline runs in — for a fork MR, the fork itself — so it carries
    none of the hazard this rule is about."""
    text = _CLEAN.replace(
        "    - indexbot validate-pr",
        '    - curl -H "JOB-TOKEN: $CI_JOB_TOKEN" https://example.com',
    )
    assert _rules(text) == []


def test_gl03_ignores_an_ordinary_non_credential_variable() -> None:
    text = _CLEAN.replace("    - indexbot validate-pr", "    - echo $CI_PROJECT_PATH")
    assert _rules(text) == []


def test_gl03_ignores_a_token_on_a_job_not_triggered_by_merge_request_event() -> None:
    """The near-miss WF-03/06 style rules all guard against: the scheduled,
    parent-only lane legitimately holds `$GITLAB_TOKEN` — it is a fork
    pipeline holding one that is the hazard."""
    text = f"""\
default:
  image: python@sha256:{_DIGEST}

indexbot-poll:
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
  script:
    - curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" https://example.com
"""
    assert _rules(text) == []


def test_gl03_does_not_see_a_rules_block_reached_only_through_extends() -> None:
    """Documented blind spot: the job's own block is read, not what
    `extends:` pulls in from a template — a job whose `merge_request_event`
    guard lives entirely on an extended template is not caught."""
    text = f"""\
default:
  image: python@sha256:{_DIGEST}

.mr-only:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"

indexbot-leaky:
  extends: .mr-only
  script:
    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com
"""
    assert _rules(text) == []


# --- parser helpers ----------------------------------------------------------


def test_job_names_excludes_reserved_top_level_keys_and_includes_hidden_templates() -> None:
    text = """\
default:
  image: foo

variables:
  X: "1"

.indexbot:
  before_script:
    - echo hi

real-job:
  script:
    - echo hi
"""
    assert job_names(text) == [".indexbot", "real-job"]


def test_job_names_of_a_pipeline_with_no_jobs_is_empty() -> None:
    assert job_names('variables:\n  X: "1"\n') == []


def test_job_names_reads_a_header_carrying_a_trailing_comment() -> None:
    assert job_names("a:  # first\nb:\n") == ["a", "b"]


def test_job_block_stops_at_the_next_top_level_key() -> None:
    text = "a:\n  script:\n    - echo a\nb:\n  script:\n    - echo b\n"
    block = job_block(text, "a")
    assert block.startswith("a:")
    assert "echo b" not in block


def test_job_block_of_the_last_job_runs_to_eof() -> None:
    text = "a:\n  script:\n    - echo a\nb:\n  script:\n    - echo b\n"
    assert job_block(text, "b").rstrip().endswith("echo b")


def test_job_block_of_a_header_with_nothing_after_it_is_just_the_header() -> None:
    """The job header is the last line in the file — the scan for a
    following top-level key has no lines left to iterate over at all."""
    text = "a:\n  script:\n    - echo a\nb:"
    assert job_block(text, "b") == "b:"


def test_job_block_is_not_ended_by_a_column_zero_comment() -> None:
    """A comment at column zero between two jobs is not a job header and
    must not cut the preceding job's block short."""
    text = "a:\n  script:\n    - echo a\n# a separator\nb:\n  script:\n    - echo b\n"
    block = job_block(text, "a")
    assert "# a separator" in block
    assert "echo b" not in block


def test_a_token_inside_a_block_scalar_script_is_still_found() -> None:
    """`script: |` is the ordinary spelling for a multi-line shell body, and
    it puts a block-scalar indicator on the key line. A section matcher
    anchored on end-of-line matched neither that nor `script: echo "$TOKEN"`,
    read the section as empty, and reported a job holding a parent credential
    as holding none — a silent clean, which is the only way a rule like this
    really fails."""
    text = """\
leak:
  image: alpine@sha256:%s
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script: |
    echo "$INDEXBOT_TOKEN" | docker login --password-stdin
""" % ("a" * 64)

    findings = check_gitlab({".gitlab-ci.yml": text})

    assert [f.rule for f in findings] == ["GL-03"]
    assert "INDEXBOT_TOKEN" in findings[0].message


def test_a_token_on_the_script_key_line_is_still_found() -> None:
    """The one-line spelling, same failure, same fix."""
    text = """\
leak:
  image: alpine@sha256:%s
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script: curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://gitlab.example/api
""" % ("a" * 64)

    findings = check_gitlab({".gitlab-ci.yml": text})

    assert [f.rule for f in findings] == ["GL-03"]


def test_a_credential_on_a_hidden_template_is_found_where_it_is_written() -> None:
    """GL-03 reads each job's own block and does not follow `extends:`, so a
    job inheriting both its `rules:` and its credential is not itself flagged.
    The template is, because a hidden `.name:` key is a top-level mapping key
    like any other — which is why the blind spot costs nothing in practice:
    the finding lands on the line an operator has to edit anyway."""
    text = """\
.creds:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    INDEXBOT_TOKEN: $PARENT_WRITE_TOKEN

victim:
  extends: .creds
  image: alpine@sha256:%s
  script:
    - echo no token named here
""" % ("a" * 64)

    findings = check_gitlab({".gitlab-ci.yml": text})

    assert [(f.rule, "creds" in f.message) for f in findings] == [("GL-03", True)]


def test_a_pages_job_is_not_exempt() -> None:
    """GitLab spells both a special deploy job and a config block `pages:`.
    Treating it as never-a-job would exempt a real one from GL-03, and the
    e2e index's own pipeline has a real one."""
    text = """\
pages:
  image: alpine@sha256:%s
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - echo "$DEPLOY_SECRET"
""" % ("a" * 64)

    findings = check_gitlab({".gitlab-ci.yml": text})

    assert [f.rule for f in findings] == ["GL-03"]


# --- GL-03 evasions found against hand-written pipelines --------------------


def test_gl03_flags_a_token_gated_only_by_a_top_level_workflow_rules() -> None:
    """`workflow: rules:` gates the whole pipeline on `merge_request_event`
    with no `rules:` on the job itself. `_MR_EVENT_RE` used to be searched
    only inside a job's own `rules:` section, so this shape was invisible."""
    text = f"""\
workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"

leak:
  image: alpine@sha256:{_DIGEST}
  script:
    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com
"""
    assert _rules(text) == ["GL-03"]


def test_gl03_a_top_level_workflow_rules_not_gating_merge_requests_is_not_a_trigger() -> None:
    text = f"""\
workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"

leak:
  image: alpine@sha256:{_DIGEST}
  script:
    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com
"""
    assert _rules(text) == []


def test_gl03_flags_a_token_on_a_job_gated_by_legacy_only() -> None:
    """`only:` is GitLab's pre-`rules:` trigger syntax — GL-03 used to read
    `rules:` only."""
    text = f"""\
leak:
  image: alpine@sha256:{_DIGEST}
  only:
    - merge_requests
  script:
    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com
"""
    assert _rules(text) == ["GL-03"]


def test_gl03_flags_a_token_on_a_job_gated_by_inline_only() -> None:
    text = f"""\
leak:
  image: alpine@sha256:{_DIGEST}
  only: [merge_requests]
  script:
    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com
"""
    assert _rules(text) == ["GL-03"]


def test_gl03_ignores_only_on_a_ref_other_than_merge_requests() -> None:
    text = f"""\
leak:
  image: alpine@sha256:{_DIGEST}
  only:
    - master
  script:
    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com
"""
    assert _rules(text) == []


def test_gl03_flags_a_token_referenced_in_after_script() -> None:
    """`after_script:` was missing from `_CREDENTIAL_SECTION_KEYS`."""
    text = f"""\
default:
  image: python@sha256:{_DIGEST}

indexbot-leaky:
  after_script:
    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - echo hi
"""
    assert _rules(text) == ["GL-03"]


def test_gl03_flags_a_token_in_the_top_level_variables_block() -> None:
    """A top-level `variables:` block is in scope for every job — `variables`
    used to be in `_RESERVED_TOP_LEVEL` and nothing ever scanned it."""
    text = f"""\
variables:
  HEADER: $DEPLOY_PASSWORD

leak:
  image: alpine@sha256:{_DIGEST}
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - echo hi
"""
    findings = check_gitlab({"x.yml": text})

    assert [f.rule for f in findings] == ["GL-03"]
    assert "DEPLOY_PASSWORD" in findings[0].message
    assert "leak" in findings[0].message


def test_gl03_ignores_top_level_variables_when_no_job_runs_on_merge_request_event() -> None:
    text = f"""\
variables:
  HEADER: $DEPLOY_PASSWORD

build:
  image: alpine@sha256:{_DIGEST}
  script:
    - echo hi
"""
    assert _rules(text) == []


def test_gl03_sees_a_job_whose_header_carries_a_yaml_anchor() -> None:
    """`deploy: &deploy` did not match `_JOB_RE` at all — the job was
    invisible to `job_names`, not merely under-checked."""
    text = f"""\
deploy: &deploy
  image: alpine@sha256:{_DIGEST}
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com
"""
    assert job_names(text) == ["deploy"]
    assert _rules(text) == ["GL-03"]


def test_gl03_flags_glpat_shaped_variable_name() -> None:
    leaky = _CLEAN.replace(
        "    - indexbot validate-pr",
        '    - curl -H "PRIVATE-TOKEN: $GLPAT" https://example.com',
    )
    assert _rules(leaky) == ["GL-03"]


def test_gl03_flags_a_deploy_key_shaped_variable_name() -> None:
    leaky = _CLEAN.replace(
        "    - indexbot validate-pr",
        '    - ssh-add <(echo "$DEPLOY_KEY")',
    )
    assert _rules(leaky) == ["GL-03"]


def test_gl03_flags_an_npm_auth_shaped_variable_name() -> None:
    leaky = _CLEAN.replace(
        "    - indexbot validate-pr",
        "    - npm config set //registry.npmjs.org/:_authToken=$NPM_AUTH",
    )
    assert _rules(leaky) == ["GL-03"]


def test_gl03_still_ignores_ci_project_path_after_widening_for_pat() -> None:
    """The one real collision the widened `PAT` term would otherwise catch:
    `$CI_PROJECT_PATH` is a GitLab predefined variable, not a credential."""
    text = _CLEAN.replace("    - indexbot validate-pr", "    - echo $CI_PROJECT_PATH")
    assert _rules(text) == []


def test_gl03_ci_commit_author_is_exempt() -> None:
    """`$CI_COMMIT_AUTHOR` is a name/email string GitLab fills in — the one
    builtin the widened `AUTH` term newly reaches."""
    text = _CLEAN.replace(
        "    - indexbot validate-pr", '    - echo "committed by $CI_COMMIT_AUTHOR"'
    )
    assert _rules(text) == []


def test_gl03_does_not_see_a_rules_block_reached_only_through_reference() -> None:
    """Documented blind spot, the same class as `extends:`: `!reference`
    pulls another key's value in verbatim, and following it needs the same
    graph resolution this line-scan deliberately does not build."""
    text = """\
.mr:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"

indexbot-leaky:
  rules: !reference [.mr, rules]
  script:
    - curl -H "PRIVATE-TOKEN: $INDEXBOT_TOKEN" https://example.com
"""
    assert _rules(text) == []


def test_top_level_section_is_not_ended_by_a_column_zero_comment() -> None:
    text = "variables:\n  X: $A\n# a separator\nleak:\n  script:\n    - echo hi\n"
    section = top_level_section(text, "variables")
    assert "X: $A" in section
    assert "leak" not in section


# --- GL-01 false positives ---------------------------------------------------


def test_gl01_allows_a_double_quoted_pinned_image() -> None:
    text = f'default:\n  image: "python@sha256:{_DIGEST}"\n'
    assert check_gitlab({"x.yml": text}) == ()


def test_gl01_allows_a_single_quoted_pinned_image() -> None:
    text = f"default:\n  image: 'python@sha256:{_DIGEST}'\n"
    assert check_gitlab({"x.yml": text}) == ()


def test_gl01_still_flags_a_quoted_floating_tag() -> None:
    text = 'default:\n  image: "python:3.13"\n'
    assert _rules(text) == ["GL-01"]


def test_gl01_ignores_an_image_line_inside_a_block_scalar_script() -> None:
    """A shell body that happens to emit a line shaped exactly like a YAML
    `image:` key — a Dockerfile heredoc, a rendered manifest, a log line —
    must not be read as a second, unpinned `image:` that nothing runs."""
    text = f"""\
default:
  image: python@sha256:{_DIGEST}

leak:
  script: |
    image: python:3.13
"""
    assert check_gitlab({"x.yml": text}) == ()


def test_gl01_resumes_scanning_after_a_block_scalar_ends() -> None:
    """The dedent that ends the block scalar is a real YAML key again — a
    genuine `image:` override right after one must still be read."""
    text = f"""\
default:
  image: python@sha256:{_DIGEST}

leak:
  script: |
    image: python:3.13
  image: oven/bun:1-alpine
"""
    assert _rules(text) == ["GL-01"]
