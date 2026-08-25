"""Tests for `cli/validate_pr.py` — the whole unprivileged PR gate as one
command.

The three shell steps this replaces were each a security control, and none of
them was testable as YAML. What is asserted here is what the comments in those
steps claimed: the diff is resolved from the right base, the base-ref bytes
land outside the workspace, and `--allow-reserved-namespace` reaches
`validate` only for a pull request that provably came from the index
repository itself.

`adapters/local_git.py`'s own suite covers the git semantics against a real
repository; this file uses a scripted `GitPort` so each test can state the
file set it is reasoning about.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ocx_indexbot.cli import validate_pr
from ocx_indexbot.core.policy import INDEX_POLICY_PATH
from ocx_indexbot.core.validate_entry import serialize_package_root
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import Owner, PackageRoot
from ocx_indexbot.ports import GitPort
from tests.fakes import FakeRegistry, InMemoryFiles

if TYPE_CHECKING:
    pass

_REPOSITORY = "oci://ghcr.io/ocx-contrib/cmake"


_POLICY_BYTES = (
    b'{"name":"ocx.sh","name_segments":2,"registry_hosts":["ghcr.io"],'
    b'"reserved_namespaces":["ocx","ocx-sh","ocx-contrib","ocx-rs"]}\n'
)
"""The public index's own policy, byte-for-byte equivalent to
`tests.fakes.make_policy()`. It is what the fake git serves at the base ref,
and therefore what `run` actually obeys."""
"""Stand-in bytes for `.github/index-policy.json`.

Only their *equality* is under test — the parsed policy every case runs under
is `make_policy()`, injected — so these need to be a plausible policy document
and nothing more.
"""


class ScriptedGit:
    """`GitPort` with both answers dictated up front.

    Records the `base_sha` and `root_glob` it was called with, because "which
    base did the command actually diff against, and with which pathspec" is
    the whole of what the two resolution helpers decide, and every `file_at`
    call as `(ref, path)`, because the base-ref reads must all come from that
    same base.

    `at_base` is seeded with the deployment policy, matching what `_run`
    writes into the head tree: the trust-direction guard reads both before
    anything else, so every case that is *not* about that guard needs the two
    copies to agree.
    """

    def __init__(
        self, changed: tuple[str, ...] = (), at_base: dict[str, bytes] | None = None
    ) -> None:
        self.changed = changed
        self.at_base = {INDEX_POLICY_PATH: _POLICY_BYTES} | ({} if at_base is None else at_base)
        self.diffed_from: str | None = None
        self.diffed_glob: str | None = None
        self.read_at: list[tuple[str, str]] = []

    def changed_package_roots(self, base_sha: str, *, root_glob: str) -> tuple[str, ...]:
        self.diffed_from = base_sha
        self.diffed_glob = root_glob
        return self.changed

    def file_at(self, ref: str, path: str) -> bytes | None:
        self.read_at.append((ref, path))
        return self.at_base.get(path)


def _root_bytes(name: str) -> bytes:
    """A minimal, canonically-serialized package root — no tags, so `validate`
    reaches every structural check without needing a registry."""
    return serialize_package_root(
        PackageRoot(
            name=name,
            repository=_REPOSITORY,
            owners=(Owner(github="alice", github_id=1),),
            status="active",
            deprecated_message=None,
            created="2026-07-17",
            desc=None,
            upstream=None,
            tags={},
        )
    )


def _args(
    *,
    base_sha: str | None = "basesha",
    offline: bool = True,
    same_repo_pr: bool = False,
    fork_pr: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        base_sha=base_sha, offline=offline, same_repo_pr=same_repo_pr, fork_pr=fork_pr
    )


def _run(
    args: argparse.Namespace,
    *,
    git: GitPort,
    files: InMemoryFiles,
    base_files: InMemoryFiles | None = None,
) -> ExitCode:
    """The policy is never injected: `run` reads it from the base ref through
    the `GitPort`, which is the whole point of `_base_ref_policy`. A case that
    wants a different policy in force sets it in the fake git's `at_base`, and
    a case about a policy-changing pull request sets a different one in
    `files`."""
    files.files.setdefault(INDEX_POLICY_PATH, _POLICY_BYTES)
    return validate_pr.run(
        args,
        git=git,
        files=files,
        registry=FakeRegistry(),
        base_files=InMemoryFiles() if base_files is None else base_files,
    )


@pytest.fixture(autouse=True)
def _no_ambient_ci(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clear every variable the provenance and base-sha sniffs read.

    Without this a developer's own shell — or a CI job running this suite —
    decides what the fail-closed tests below observe, which is exactly the
    kind of ambient dependency that makes a security default untestable.
    """
    for name in (
        "GITHUB_ACTIONS",
        "GITHUB_BASE_REF",
        "GITHUB_EVENT_PATH",
        "GITHUB_REPOSITORY",
        "GITHUB_STEP_SUMMARY",
        "INDEXBOT_BASE_SHA",
        "CI_MERGE_REQUEST_DIFF_BASE_SHA",
        "CI_MERGE_REQUEST_PROJECT_PATH",
        "CI_MERGE_REQUEST_SOURCE_PROJECT_PATH",
        "CI_PROJECT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


# --- base-sha resolution -------------------------------------------------------------


def test_base_sha_flag_wins_over_every_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is what a hand-rolled pipeline sets, so nothing the runner
    happens to export may override it."""
    monkeypatch.setenv("INDEXBOT_BASE_SHA", "from-env")
    git = ScriptedGit()
    assert _run(_args(base_sha="from-flag"), git=git, files=InMemoryFiles()) == ExitCode.OK
    assert git.diffed_from == "from-flag"


def test_base_sha_falls_back_to_indexbot_base_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """`$INDEXBOT_BASE_SHA` is the forge-independent escape hatch, and it is
    read before either forge's own variable."""
    monkeypatch.setenv("INDEXBOT_BASE_SHA", "explicit")
    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", "gitlab")
    git = ScriptedGit()
    assert _run(_args(base_sha=None), git=git, files=InMemoryFiles()) == ExitCode.OK
    assert git.diffed_from == "explicit"


def test_base_sha_falls_back_to_the_gitlab_merge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitLab sets `$CI_MERGE_REQUEST_DIFF_BASE_SHA` on every merge-request
    pipeline — the same variable the generated `.gitlab-ci` job used."""
    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", "gitlab-base")
    git = ScriptedGit()
    assert _run(_args(base_sha=None), git=git, files=InMemoryFiles()) == ExitCode.OK
    assert git.diffed_from == "gitlab-base"


def test_base_sha_falls_back_to_origin_of_the_github_base_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub exports no merge-base sha, only the target branch name, so the
    fallback is `origin/<branch>` — which needs `fetch-depth: 0`."""
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    git = ScriptedGit()
    assert _run(_args(base_sha=None), git=git, files=InMemoryFiles()) == ExitCode.OK
    assert git.diffed_from == "origin/main"


def test_base_sha_unresolvable_is_a_named_validation_error() -> None:
    """No flag and no recognized variable is a configuration error naming
    every accepted input — never a silent diff against nothing, which would
    pass the required check with zero roots validated."""
    with pytest.raises(ValidationError, match="cannot determine the pull request's base commit"):
        _run(_args(base_sha=None), git=ScriptedGit(), files=InMemoryFiles())


# --- the policy this gate obeys ------------------------------------------------------


def test_a_head_authored_policy_cannot_neuter_the_pathspec() -> None:
    """The trust-direction bug this guard exists for, in its exact shape.

    `validate.yml` checks out `pull_request.head.sha`, so the
    `.github/index-policy.json` sitting in the checkout is the fork's.
    Declaring `"name_segments": 3` would make `root_glob` select
    `p/*/*/*.json`, under which the fork's own two-segment `p/ocx/tool.json`
    matches nothing - zero changed roots, the "No package-root changes"
    notice, exit `0`, and the required context green having validated a claim
    on a reserved brand segment.

    The head's copy is never parsed, so the pathspec is the base ref's
    `p/*/*.json`, the root IS selected, and the reserved-segment refusal it
    was trying to dodge is what actually happens.
    """
    three_segments = _POLICY_BYTES.replace(b'"name_segments":2', b'"name_segments":3')
    files = InMemoryFiles(
        files={"p/ocx/tool.json": _root_bytes("ocx.sh/ocx/tool"), INDEX_POLICY_PATH: three_segments}
    )
    git = ScriptedGit(changed=("p/ocx/tool.json",))

    assert _run(_args(fork_pr=True), git=git, files=files) == ExitCode.VALIDATION_FAILURE
    assert git.diffed_glob == "p/*/*.json", "the base ref's segment count, not the head's"


def test_a_pull_request_may_still_propose_a_new_policy() -> None:
    """`.github/index-policy.json` is a committed file rather than a
    settings-page variable *so that* widening it takes a reviewed pull request
    (ADR-4 BD-3). A gate that refused every pull request touching it would
    leave direct-push-to-default as the only way to change it - the control
    inverted. The proposal is judged under the policy currently in force and
    takes effect when it merges.
    """
    widened = _POLICY_BYTES.replace(
        b'"registry_hosts":["ghcr.io"]', b'"registry_hosts":["ghcr.io","registry.gitlab.com"]'
    )
    files = InMemoryFiles(
        files={
            "p/kitware/cmake.json": _root_bytes("ocx.sh/kitware/cmake"),
            INDEX_POLICY_PATH: widened,
        }
    )
    git = ScriptedGit(changed=("p/kitware/cmake.json",))

    assert _run(_args(), git=git, files=files) == ExitCode.OK
    assert git.diffed_glob == "p/*/*.json"


def test_a_base_ref_with_no_policy_file_is_refused() -> None:
    """A brand-new index, or a pull request that ADDS the policy file. Either
    way the base ref states no policy, so there is none in force for this gate
    to obey — and adopting the incoming branch's is the trust direction the
    guard exists to reverse. The file lands on the default branch, by the
    operator who owns it."""
    git = ScriptedGit()
    del git.at_base[INDEX_POLICY_PATH]

    with pytest.raises(ValidationError) as caught:
        _run(_args(base_sha="origin/main"), git=git, files=InMemoryFiles())

    # An operator reading this in a job log has to know which file to look at
    # and which ref stated no policy - the two facts a bare "policy missing"
    # would make them go find.
    message = str(caught.value)
    assert f"the base ref has no {INDEX_POLICY_PATH}" in message
    assert "origin/main" in message


def test_an_unchanged_policy_file_needs_no_special_case() -> None:
    """The overwhelmingly common case: the pull request touches package roots
    and leaves the policy alone. Byte-identical copies mean the injected
    policy is provably the base ref's, so the command proceeds on it."""
    files = InMemoryFiles(files={"p/kitware/cmake.json": _root_bytes("ocx.sh/kitware/cmake")})
    git = ScriptedGit(changed=("p/kitware/cmake.json",))

    assert _run(_args(), git=git, files=files) == ExitCode.OK
    assert git.diffed_glob == "p/*/*.json"


# --- the pathspec --------------------------------------------------------------------


def test_the_root_glob_comes_from_the_declared_segment_count() -> None:
    """`name_segments` is per-deployment (0.2.0), so the pathspec is derived
    from the policy rather than hardcoded at two."""
    git = ScriptedGit(
        at_base={
            INDEX_POLICY_PATH: _POLICY_BYTES.replace(b'"name_segments":2', b'"name_segments":3')
        }
    )
    _run(_args(), git=git, files=InMemoryFiles())
    assert git.diffed_glob == "p/*/*/*.json"


# --- the empty diff ------------------------------------------------------------------


def test_no_changed_roots_exits_ok_with_a_notice(capsys: pytest.CaptureFixture[str]) -> None:
    """A docs-only PR is a pass, not a skip nobody can see: the same notice the
    workflow's shell step emitted still reaches the log."""
    assert _run(_args(), git=ScriptedGit(), files=InMemoryFiles()) == ExitCode.OK
    assert "nothing to validate" in capsys.readouterr().err


def test_the_empty_diff_notice_is_a_github_annotation_on_github(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On GitHub Actions the notice is a workflow command, so it surfaces in
    the checks UI rather than only in the log."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _run(_args(), git=ScriptedGit(), files=InMemoryFiles())
    assert capsys.readouterr().out.startswith("::notice title=indexbot validate-pr::")


# --- base-ref materialization --------------------------------------------------------


def test_base_ref_bytes_land_in_the_base_tree_not_the_workspace() -> None:
    """`validate` byte-compares the PR-head tree against its own canonical
    serialization, so a base-ref copy written into that tree would fail every
    changed root. It goes into the separate base tree, untouched."""
    head = _root_bytes("ocx.sh/kitware/cmake")
    base = _root_bytes("ocx.sh/kitware/cmake").replace(b"2026-07-17", b"2020-01-01")
    files = InMemoryFiles(files={"p/kitware/cmake.json": head})
    base_files = InMemoryFiles()
    git = ScriptedGit(changed=("p/kitware/cmake.json",), at_base={"p/kitware/cmake.json": base})

    assert _run(_args(), git=git, files=files, base_files=base_files) == ExitCode.OK
    assert base_files.read_bytes("p/kitware/cmake.json") == base
    assert files.read_bytes("p/kitware/cmake.json") == head
    # Every base-ref read comes from the base this run resolved — the policy
    # comparison included, and it happens first.
    assert git.read_at == [("basesha", INDEX_POLICY_PATH), ("basesha", "p/kitware/cmake.json")]


def test_a_root_absent_at_the_base_ref_is_simply_not_written() -> None:
    """A root the PR creates has no base-ref copy. Nothing is written for it,
    so `validate` sees no base bytes and reads it as a new claim — the
    fail-closed half of ADR-2 ND-4."""
    files = InMemoryFiles(files={"p/kitware/cmake.json": _root_bytes("ocx.sh/kitware/cmake")})
    base_files = InMemoryFiles()
    git = ScriptedGit(changed=("p/kitware/cmake.json",))

    assert _run(_args(), git=git, files=files, base_files=base_files) == ExitCode.OK
    assert base_files.list_files("p") == []


# --- the reserved-namespace provenance gate ------------------------------------------


def _reserved_claim() -> tuple[InMemoryFiles, ScriptedGit]:
    """A pull request CLAIMING a root under a reserved brand segment, with no
    base-ref copy — admitted only with `--allow-reserved-namespace`."""
    path = "p/ocx/tool.json"
    files = InMemoryFiles(files={path: _root_bytes("ocx.sh/ocx/tool")})
    return files, ScriptedGit(changed=(path,))


def test_a_fork_pull_request_may_not_claim_a_reserved_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate this whole lane exists for: a fork must never publish under
    the index's own brand and be believed by every client resolving through
    it. `head.repo.full_name` names the fork, so the flag is withheld and
    `check_namespace_not_reserved` rejects the claim.
    """
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"head": {"repo": {"full_name": "mallory/index"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    files, git = _reserved_claim()

    assert _run(_args(), git=git, files=files) == ExitCode.VALIDATION_FAILURE


def test_a_same_repo_github_pull_request_may_claim_a_reserved_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator's own branch — `head.repo.full_name == $GITHUB_REPOSITORY`
    — gets the carve-out, which is what lets first-party roots exist at all."""
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"head": {"repo": {"full_name": "ocx-sh/index"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    files, git = _reserved_claim()

    assert _run(_args(), git=git, files=files) == ExitCode.OK


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"pull_request": {"head": {"repo": None}}}, "a deleted fork leaves head.repo null"),
        ({"pull_request": {"head": {}}}, "head carries no repo at all"),
        ({"pull_request": {"head": {"repo": {"full_name": 7}}}}, "full_name is not a string"),
        ({"action": "opened"}, "the payload is not a pull-request event"),
        ([], "the payload is not even an object"),
    ],
)
def test_an_unreadable_github_event_payload_is_treated_as_a_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: object, why: str
) -> None:
    """Every shape that does not positively prove same-repo provenance reads
    as a fork. `why` names the shape; the assertion is the same each time,
    because fail-closed means there is exactly one safe answer."""
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    files, git = _reserved_claim()

    assert _run(_args(), git=git, files=files) == ExitCode.VALIDATION_FAILURE, why


def test_a_missing_or_malformed_event_file_is_treated_as_a_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`$GITHUB_EVENT_PATH` pointing at nothing readable is not an excuse to
    open the brand — it is the same "unknown provenance" answer as a fork."""
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    files, git = _reserved_claim()

    assert _run(_args(), git=git, files=files) == ExitCode.VALIDATION_FAILURE


def test_a_same_project_gitlab_merge_request_may_claim_a_reserved_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitLab's source project compared against the merge request's TARGET
    project. `$CI_PROJECT_PATH` would be the wrong right-hand side — a fork
    merge request's pipeline runs in the fork, so it equals the source project
    for every merge request and the gate would always open."""
    monkeypatch.setenv("CI_MERGE_REQUEST_SOURCE_PROJECT_PATH", "ocx-sh/index")
    monkeypatch.setenv("CI_MERGE_REQUEST_PROJECT_PATH", "ocx-sh/index")
    monkeypatch.setenv("CI_PROJECT_PATH", "ocx-sh/index")
    files, git = _reserved_claim()

    assert _run(_args(), git=git, files=files) == ExitCode.OK


def test_a_fork_gitlab_merge_request_may_not_claim_a_reserved_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the comparison that granted the carve-out to every merge
    request (fixed in the GitLab template by `de2da78`).

    `$CI_PROJECT_PATH` names the project whose runner is EXECUTING the
    pipeline. A fork merge request's pipeline runs in the fork, so
    `$CI_PROJECT_PATH` is the fork's own path — identical to
    `$CI_MERGE_REQUEST_SOURCE_PROJECT_PATH`, which is why comparing those two
    is true for every merge request, fork or not, and hands
    `--allow-reserved-namespace` to anyone who opens one.
    `$CI_MERGE_REQUEST_PROJECT_PATH` is the TARGET project regardless of where
    the pipeline executes, so it is the only right-hand side that can tell the
    two apart.

    The environment below is the real fork shape, and the first assertion
    pins the trap mechanically rather than in prose: under the wrong variable
    this same environment reads as same-repo.
    """
    monkeypatch.setenv("CI_MERGE_REQUEST_SOURCE_PROJECT_PATH", "mallory/index")
    monkeypatch.setenv("CI_MERGE_REQUEST_PROJECT_PATH", "ocx-sh/index")
    monkeypatch.setenv("CI_PROJECT_PATH", "mallory/index")
    files, git = _reserved_claim()

    assert os.environ["CI_MERGE_REQUEST_SOURCE_PROJECT_PATH"] == os.environ["CI_PROJECT_PATH"]
    assert _run(_args(), git=git, files=files) == ExitCode.VALIDATION_FAILURE


def test_an_unrecognized_environment_is_treated_as_a_fork() -> None:
    """A laptop, or a CI this bot has never heard of: neither forge's
    variables resolve, so the brand stays closed."""
    files, git = _reserved_claim()
    assert _run(_args(), git=git, files=files) == ExitCode.VALIDATION_FAILURE


@pytest.mark.parametrize(
    "present", ["CI_MERGE_REQUEST_SOURCE_PROJECT_PATH", "CI_MERGE_REQUEST_PROJECT_PATH"]
)
def test_a_half_set_gitlab_environment_is_treated_as_a_fork(
    monkeypatch: pytest.MonkeyPatch, present: str
) -> None:
    """Both variables must be present and non-empty. Either one alone proves
    nothing — comparing a value against an absent one must never be the thing
    that opens the brand, in either direction."""
    monkeypatch.setenv(present, "ocx-sh/index")
    files, git = _reserved_claim()
    assert _run(_args(), git=git, files=files) == ExitCode.VALIDATION_FAILURE


def test_same_repo_pr_flag_overrides_the_environment_sniff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--same-repo-pr` exists for a pipeline this bot did not generate, whose
    variables it therefore cannot know. It wins over a sniff that would say
    fork."""
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"head": {"repo": {"full_name": "mallory/index"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    files, git = _reserved_claim()

    assert _run(_args(same_repo_pr=True), git=git, files=files) == ExitCode.OK


def test_fork_pr_flag_overrides_the_environment_sniff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """And the other direction, which is the one worth having: an operator can
    force the strict reading even where the environment claims same-repo."""
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"head": {"repo": {"full_name": "ocx-sh/index"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    files, git = _reserved_claim()

    assert _run(_args(fork_pr=True), git=git, files=files) == ExitCode.VALIDATION_FAILURE


def test_the_two_provenance_flags_are_mutually_exclusive() -> None:
    """Neither reading has to be defined, because argparse refuses the pair at
    parse time."""
    parser = argparse.ArgumentParser()
    validate_pr.add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--same-repo-pr", "--fork-pr"])


def test_a_fork_may_still_refresh_a_root_already_under_a_reserved_segment() -> None:
    """The carve-out is about CLAIMING, not UPDATING. `ocx package announce
    --fork` can open nothing but fork PRs, so an announce-shaped change to a
    root already committed under a reserved segment is admitted by the
    base-ref bytes alone — no flag, no provenance."""
    path = "p/ocx/tool.json"
    committed = _root_bytes("ocx.sh/ocx/tool")
    files = InMemoryFiles(files={path: committed})
    base_files = InMemoryFiles()
    git = ScriptedGit(changed=(path,), at_base={path: committed})

    assert _run(_args(), git=git, files=files, base_files=base_files) == ExitCode.OK


# --- failure presentation ------------------------------------------------------------


def test_a_failure_emits_an_annotation_and_a_fenced_summary_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The presentation the shell wrapper did: an `::error` the checks UI
    surfaces, plus the reason on the job-summary page a publisher actually
    reads. The per-root message is derived from PR content, so it appears only
    inside the fenced block — never in the annotation's title (ADR-4 BD-4).
    """
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    files = InMemoryFiles(files={"p/kitware/cmake.json": b"not json"})
    git = ScriptedGit(changed=("p/kitware/cmake.json",))

    assert _run(_args(), git=git, files=files) == ExitCode.VALIDATION_FAILURE

    stdout = capsys.readouterr().out
    assert "::error title=indexbot validate-pr failed::" in stdout
    body = summary.read_text(encoding="utf-8")
    assert body.startswith("## indexbot validate-pr failed")
    assert "```\np/kitware/cmake.json: VALIDATION_FAILURE" in body


def test_a_pass_writes_no_annotation_and_no_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A green run is silent on both surfaces — an annotation on every run is
    an annotation nobody reads."""
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    files = InMemoryFiles(files={"p/kitware/cmake.json": _root_bytes("ocx.sh/kitware/cmake")})
    git = ScriptedGit(changed=("p/kitware/cmake.json",))

    assert _run(_args(), git=git, files=files) == ExitCode.OK
    assert capsys.readouterr().out == ""
    assert not summary.exists()


def test_the_worst_exit_code_across_every_changed_root_wins() -> None:
    """One bad root fails the whole pull request, and the reported code is the
    worst seen — `validate`'s own aggregation, reused rather than reimplemented."""
    files = InMemoryFiles(
        files={
            "p/kitware/cmake.json": _root_bytes("ocx.sh/kitware/cmake"),
            "p/acme/tool.json": b"not json",
        }
    )
    git = ScriptedGit(changed=("p/kitware/cmake.json", "p/acme/tool.json"))

    assert _run(_args(), git=git, files=files) == ExitCode.VALIDATION_FAILURE
