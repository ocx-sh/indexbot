"""`cli/_wiring.py` — production DI-construction unit tests, plus end-to-end
`cli/main.main()` tests that swap real `adapters/*` for `tests/fakes/` at the
wiring seam (monkeypatching the adapter-constructor names `cli/_wiring.py`
calls, never `main.main`'s own `_DISPATCH` — that seam is `test_main.py`'s,
this file exercises the real production dispatch table end to end).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from ocx_indexbot.adapters.github_api import GitHubApi
from ocx_indexbot.adapters.gitlab_api import GitLabApi
from ocx_indexbot.adapters.local_files import LocalFiles
from ocx_indexbot.adapters.local_git import LocalGit
from ocx_indexbot.adapters.registry_v2 import GHCR_HOST, GITLAB_HOST, OCX_SH_HOST, OCX_SH_REALM
from ocx_indexbot.cli import _wiring, announce
from ocx_indexbot.cli import main as main_module
from ocx_indexbot.core.observe import observe
from ocx_indexbot.core.policy import INDEX_POLICY_PATH
from ocx_indexbot.core.validate_entry import serialize_package_root
from ocx_indexbot.errors import TransientError, ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import Owner, PackageRoot, PullRequestHeadMatch, PullRequestInfo, TagEntry
from tests.fakes import FakeGitHub, FakeRegistry, FixedClock, InMemoryFiles

_NS = "kitware"
_PKG = "cmake"
_REPO = "oci://ghcr.io/kitware/cmake"
_ROOT_PATH = f"p/{_NS}/{_PKG}.json"
_OWNER = Owner(github="alice", github_id=1)
_POLICY_BYTES = (
    b'{"name": "ocx.sh", "name_segments": 2, "registry_hosts": ["ghcr.io"], '
    b'"reserved_namespaces": ["ocx", "ocx-sh", "ocx-contrib", "ocx-rs"]}\n'
)
"""The public index's committed policy, verbatim. `reserved_namespaces` is
load-bearing here and not decoration: brand segments moved out of the package
into policy in 0.2.0, so a fixture that omitted them would let
`p/ocx/cli.json` through and quietly retire the ND-4 wiring assertion below."""


# --- `_require_env` -----------------------------------------------------------


def test_require_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INDEXBOT_TEST_VAR", raising=False)
    with pytest.raises(RuntimeError, match="INDEXBOT_TEST_VAR"):
        _wiring._require_env("INDEXBOT_TEST_VAR")  # pyright: ignore[reportPrivateUsage]


def test_require_env_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDEXBOT_TEST_VAR", "")
    with pytest.raises(RuntimeError, match="INDEXBOT_TEST_VAR"):
        _wiring._require_env("INDEXBOT_TEST_VAR")  # pyright: ignore[reportPrivateUsage]


def test_require_env_present_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDEXBOT_TEST_VAR", "value")
    assert _wiring._require_env("INDEXBOT_TEST_VAR") == "value"  # pyright: ignore[reportPrivateUsage]


# --- `_repo_root` --------------------------------------------------------------


def test_repo_root_defaults_to_current_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    assert _wiring._repo_root() == Path(".")  # pyright: ignore[reportPrivateUsage]


def test_repo_root_reads_github_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", "/checkout")
    assert _wiring._repo_root() == Path("/checkout")  # pyright: ignore[reportPrivateUsage]


# --- `_forge_api` / `_forge_kind` -------------------------------------------------


def test_forge_api_reads_owner_repo_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    api = _wiring._forge_api()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(api, GitHubApi)
    assert api.owner == "ocx-sh"
    assert api.repo == "index"
    assert api.token == "secret-token"  # noqa: S105 - test fixture, not a real credential


def test_forge_api_missing_repository_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    with pytest.raises(RuntimeError, match="GITHUB_REPOSITORY"):
        _wiring._forge_api()  # pyright: ignore[reportPrivateUsage]


def test_forge_api_on_gitlab_ci_builds_the_gitlab_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring assertion for `adapters/gitlab_api.py`: without it the whole
    adapter is unreachable from any shipped entrypoint, and coverage cannot
    tell the difference between wired and dead."""
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_PROJECT_ID", "1234")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-secret")
    monkeypatch.setenv("CI_API_V4_URL", "https://gitlab.corp.internal/api/v4")

    api = _wiring._forge_api()  # pyright: ignore[reportPrivateUsage]

    assert isinstance(api, GitLabApi)
    assert api.project == "1234"
    assert api.base_url == "https://gitlab.corp.internal/api/v4"


def test_forge_api_on_gitlab_defaults_to_gitlab_com(monkeypatch: pytest.MonkeyPatch) -> None:
    """gitlab.com is the only instance whose API address is knowable without
    being told; a self-hosted runner always sets `$CI_API_V4_URL` itself."""
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_PROJECT_ID", "1234")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-secret")
    monkeypatch.delenv("CI_API_V4_URL", raising=False)

    api = _wiring._forge_api()  # pyright: ignore[reportPrivateUsage]

    assert isinstance(api, GitLabApi)
    assert api.base_url == "https://gitlab.com/api/v4"


def test_forge_api_on_gitlab_without_a_write_token_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`$CI_JOB_TOKEN` cannot write labels, notes or MRs, so there is nothing
    to fall back to — a GitLab deployment must supply its own token."""
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_PROJECT_ID", "1234")
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="GITLAB_TOKEN"):
        _wiring._forge_api()  # pyright: ignore[reportPrivateUsage]


def test_an_explicit_forge_flag_overrides_the_runner_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`announce` is the one subcommand a human runs from their own machine,
    where neither runner variable is set — and where the fallback would
    otherwise send a GitLab publisher at github.com."""
    monkeypatch.setenv("GITLAB_CI", "true")
    github_args = argparse.Namespace(forge="github")
    gitlab_args = argparse.Namespace(forge="gitlab")

    assert _wiring._forge_kind(github_args) == "github"  # pyright: ignore[reportPrivateUsage]
    assert _wiring._forge_kind(gitlab_args) == "gitlab"  # pyright: ignore[reportPrivateUsage]


def test_forge_api_falls_through_to_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    assert isinstance(_wiring._forge_api(), GitHubApi)  # pyright: ignore[reportPrivateUsage]


def test_forge_api_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        _wiring._forge_api()  # pyright: ignore[reportPrivateUsage]


# --- DISPATCH table shape -------------------------------------------------------


def test_dispatch_registers_exactly_the_fifteen_subcommands() -> None:
    assert set(_wiring.DISPATCH) == {
        "announce",
        "ci",
        "reconcile",
        "validate",
        "validate-pr",
        "render",
        "seed-import",
        "classify-pr",
        "governance-check",
        "governance-gate",
        "governance-poll",
        "label-failed-run",
        "stale",
        "workflows-check",
        "schema",
    }


def test_schema_is_reachable_through_the_shipped_entrypoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`schema` was the one subcommand no test ever invoked through dispatch.

    Its registration is a plain assignment and `DISPATCH` is copied by value,
    so the risk is small — but "small" is the argument for every dead
    entrypoint, and comparing key sets proves the key exists, never that the
    value behind it runs. This is the one assertion the other twelve have.
    """
    assert main_module.main(["schema"]) == int(ExitCode.OK)

    printed = capsys.readouterr().out
    assert '"$schema"' in printed, "the policy schema itself, not a summary of it"


def test_workflows_check_is_wired_to_a_repo_root_file_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shipped entrypoint really reaches `cli/workflows_check.run` with a
    `LocalFiles` rooted at the checkout — coverage of the module alone cannot
    tell a wired subcommand from a dead one."""
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    seen: dict[str, object] = {}

    def _spy(args: argparse.Namespace, *, files: object) -> ExitCode:
        seen["args"] = args
        seen["files"] = files
        return ExitCode.OK

    monkeypatch.setattr(_wiring.workflows_check, "run", _spy)
    namespace = argparse.Namespace(dir=".github/workflows", owner=None, forge="github")

    assert _wiring.DISPATCH["workflows-check"](namespace) == ExitCode.OK
    assert seen["args"] is namespace
    assert isinstance(seen["files"], LocalFiles)


def test_validate_pr_is_wired_to_local_git_and_an_out_of_tree_base_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shipped entrypoint really reaches `cli/validate_pr.run`, with a
    `LocalGit` over the checkout and a base-ref `FilePort` rooted **outside**
    it.

    That last part is the assertion worth having: `validate` byte-compares the
    PR-head tree against its own canonical serialization, so a base tree
    anywhere inside the checkout would fail every changed root — and no amount
    of coverage on `cli/validate_pr.py` can see which root the wiring handed
    it.
    """
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    (workspace / ".github").mkdir()
    (workspace / INDEX_POLICY_PATH).write_bytes(_POLICY_BYTES)
    seen: dict[str, object] = {}

    def _spy(
        args: argparse.Namespace,
        *,
        git: object,
        files: object,
        registry: object,
        base_files: object,
    ) -> ExitCode:
        # No `policy=`: this is the one job that checks out PR-head content, so
        # the wiring must NOT hand it a policy parsed from that checkout. The
        # signature is the assertion — a wiring that reintroduced
        # `policy=_local_policy(files)` would fail here with a TypeError rather
        # than quietly obey the pull request's own copy.
        seen.update(args=args, git=git, files=files, registry=registry, base_files=base_files)
        return ExitCode.OK

    monkeypatch.setattr(_wiring.validate_pr, "run", _spy)
    namespace = argparse.Namespace(base_sha=None, offline=False, same_repo_pr=False, fork_pr=False)

    assert _wiring.DISPATCH["validate-pr"](namespace) == ExitCode.OK
    assert seen["args"] is namespace
    git = seen["git"]
    assert isinstance(git, LocalGit)
    assert git.repo == workspace
    files = seen["files"]
    assert isinstance(files, LocalFiles)
    assert files.root == workspace.resolve()
    base_files = seen["base_files"]
    assert isinstance(base_files, LocalFiles)
    assert not base_files.root.is_relative_to(workspace.resolve())


def test_main_dispatch_is_seeded_from_wiring_dispatch() -> None:
    assert set(main_module._DISPATCH) == set(_wiring.DISPATCH)  # pyright: ignore[reportPrivateUsage]


# --- `_index_policy` (deployment policy + no-adapter guard) ------------------


def test_index_policy_returns_the_committed_policy() -> None:
    assert _wiring._index_policy(_POLICY_BYTES).registry_hosts == frozenset({"ghcr.io"})  # pyright: ignore[reportPrivateUsage]


def test_index_policy_missing_policy_file_fails_closed() -> None:
    """No policy file is a hard stop, not a silent fall back to the public
    index's `ghcr.io` — an index copy that never stated a policy says so."""
    with pytest.raises(ValidationError, match="no committed policy"):
        _wiring._index_policy(None)  # pyright: ignore[reportPrivateUsage]


def test_index_policy_rejects_a_host_no_adapter_can_serve() -> None:
    """The trap this guard exists to close: allowlisting a host with no
    `RegistryPort` would produce roots that validate and then cannot be
    fetched. Refused at wiring time, naming the missing adapter."""
    policy = b'{"name": "ocx.sh", "name_segments": 2, "registry_hosts": ["harbor.corp.internal"]}'
    with pytest.raises(ValidationError, match="no registry adapter can serve"):
        _wiring._index_policy(policy)  # pyright: ignore[reportPrivateUsage]


def test_index_policy_rejects_an_unservable_host_alongside_a_servable_one() -> None:
    """Partial coverage is still a trap — one bad host poisons the whole
    policy, it is not silently filtered down to the servable subset."""
    policy = (
        b'{"name": "ocx.sh", "name_segments": 2, '
        b'"registry_hosts": ["ghcr.io", "harbor.corp.internal"]}'
    )
    with pytest.raises(ValidationError, match=r"harbor\.corp\.internal"):
        _wiring._index_policy(policy)  # pyright: ignore[reportPrivateUsage]


def test_adapter_hosts_matches_the_registry_adapters_that_exist() -> None:
    """The servable-host set is exactly the hosts `_registry()` wires a client
    for. This asserts the set stays honest: growing it without wiring a client
    re-opens the gap the guard closes."""
    assert frozenset({GHCR_HOST, OCX_SH_HOST, GITLAB_HOST}) == _wiring.REGISTRY_ADAPTER_HOSTS
    assert set(_wiring._registry().by_host) == _wiring.REGISTRY_ADAPTER_HOSTS  # pyright: ignore[reportPrivateUsage]


def test_registry_wires_the_ocx_sh_token_endpoint() -> None:
    """`ocx.sh` is Artifactory-backed: its pull tokens come from the realm its
    own `401` advertises, not from `https://ocx.sh/token` (which 404s). A
    client wired with GHCR's `{base_url}/token` default would fail every read
    against it."""
    client = _wiring._registry().by_host[OCX_SH_HOST]  # pyright: ignore[reportPrivateUsage]
    assert client.host == OCX_SH_HOST
    assert client.base_url == "https://ocx.sh"
    assert client.realm == OCX_SH_REALM


def test_the_privileged_policy_read_follows_the_branch_the_request_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment whose default branch is not `main` — `master`, `trunk` —
    had its policy read at the literal string `main` and got the fail-closed
    "no policy" refusal on every pull request. Nothing in
    `.github/index-policy.json` names the default branch, so the runner's own
    variable is where it comes from. It is also the ref `cli/validate_pr.py`
    diffs against, so both halves of one gate judge one request under one
    policy."""
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("GITHUB_BASE_REF", "trunk")
    tag_content = _observed_content_digest("1.0.0")
    root = _root({"1.0.0": TagEntry(content=tag_content, observed="2026-07-17T00:00:00Z")})
    info = PullRequestInfo(
        number=1, base_sha="base-sha", head_sha="head-sha", changed_paths=(_ROOT_PATH,)
    )
    github = FakeGitHub(
        files={(_ROOT_PATH, "head-sha"): serialize_package_root(root)},
        pull_request_info={1: info},
    )
    _patch_adapters(monkeypatch, github=github)
    # `_patch_adapters` seeds the policy at "main". This deployment has no
    # such branch, so the read has to find it where the runner says.
    del github.files[(INDEX_POLICY_PATH, "main")]
    github.files[(INDEX_POLICY_PATH, "trunk")] = _POLICY_BYTES

    assert main_module.main(["classify-pr", "--pr-number", "1"]) == ExitCode.OK


def test_the_base_ref_falls_back_to_main_off_a_request_event() -> None:
    """`reconcile` and `stale` run on a schedule, where no forge sets a
    target-branch variable. `main` is the fallback, not a required input."""
    assert _wiring._base_ref({}) == announce.BASE_REF  # pyright: ignore[reportPrivateUsage]


def test_an_explicit_base_ref_env_beats_the_forge_s_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`INDEXBOT_BASE_REF` is the escape hatch for a pipeline neither forge
    variable fits — the same shape `validate-pr`'s `INDEXBOT_BASE_SHA` has."""
    environ = {"INDEXBOT_BASE_REF": "release", "GITHUB_BASE_REF": "trunk"}
    assert _wiring._base_ref(environ) == "release"  # pyright: ignore[reportPrivateUsage]


def test_gitlab_s_target_branch_variable_is_read_too() -> None:
    """GitLab sets no `GITHUB_BASE_REF`; its merge-request pipelines carry the
    target branch under its own name."""
    environ = {"CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "master"}
    assert _wiring._base_ref(environ) == "master"  # pyright: ignore[reportPrivateUsage]


def test_announce_reads_the_index_policy_at_the_ref_it_was_told_to_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--base-ref` is the flag a publisher passes when the index's default
    branch is not `main`, and the policy governing that announce is the copy
    committed *there*. Reading it at the constant instead sent every publisher
    on such an index the fail-closed refusal."""
    tag = "1.0.0"
    committed = _root({})
    registry = FakeRegistry(tags={_REPO: [tag]}, manifests={(_REPO, tag): _index()})
    github = FakeGitHub(files={(_ROOT_PATH, "master"): serialize_package_root(committed)})
    files = InMemoryFiles()
    _patch_adapters(monkeypatch, registry=registry, github=github, files=files)
    # This index's default branch is `master`; `_patch_adapters` seeds the
    # policy at `main`, which here does not exist.
    del github.files[(INDEX_POLICY_PATH, "main")]
    github.files[(INDEX_POLICY_PATH, "master")] = _POLICY_BYTES

    result = main_module.main(
        [
            "announce",
            "--index-repo",
            "ocx-sh/index",
            "--package",
            f"{_NS}/{_PKG}",
            "--tags",
            tag,
            "--base-ref",
            "master",
            "--out",
            "dist",
        ]
    )

    assert result == ExitCode.OK
    assert files.exists(f"dist/{_ROOT_PATH}")


def test_local_policy_reads_the_checkout_copy() -> None:
    files = InMemoryFiles(files={INDEX_POLICY_PATH: _POLICY_BYTES})
    policy = _wiring._local_policy(files)  # pyright: ignore[reportPrivateUsage]
    assert policy.registry_hosts == frozenset({"ghcr.io"})
    assert (policy.name, policy.name_segments) == ("ocx.sh", 2)


def test_validate_without_a_policy_file_exits_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the real dispatch: the guard fires before the
    subcommand does any work, so the run fails on the policy, not later."""

    def _empty_checkout(**_: object) -> InMemoryFiles:
        """A checkout with no policy file at all — `_patch_adapters` seeds one
        into its own double, so this replaces it after the fact."""
        return InMemoryFiles()

    _patch_adapters(monkeypatch, files=InMemoryFiles())
    monkeypatch.setattr(_wiring, "LocalFiles", _empty_checkout)

    assert main_module.main(["validate", _ROOT_PATH, "--offline"]) == ExitCode.VALIDATION_FAILURE


# --- fixture helpers (DAMP within this file, per CONTRACTS.md §2) --------------


def _root(tags: dict[str, TagEntry]) -> PackageRoot:
    return PackageRoot(
        name=f"ocx.sh/{_NS}/{_PKG}",
        repository=_REPO,
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags),
    )


def _index() -> dict[str, object]:
    """The only manifest shape this index records — an OCI image index
    (D4(a)). Matches `tests/cli/test_validate.py`'s `_index` helper."""
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "platform": {"architecture": "amd64", "os": "linux"},
                "digest": "sha256:" + "9" * 64,
            }
        ],
    }


def _observed_content_digest(tag: str) -> str:
    """The exact `Observation.content_digest` `observe()` computes for a
    single-tag, single-platform manifest — used to seed a committed root's
    `TagEntry.content` so a later `observe()` call over the same fake
    registry state reproduces byte-identical output (a genuine no-op diff),
    matching `tests/cli/test_announce.py`'s established fixture pattern.
    """
    registry = FakeRegistry(tags={_REPO: [tag]}, manifests={(_REPO, tag): _index()})
    (observation,) = observe(_REPO, registry)
    return observation.content_digest


def _patch_adapters(
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry: FakeRegistry | None = None,
    github: FakeGitHub | None = None,
    files: InMemoryFiles | None = None,
    clock: FixedClock | None = None,
) -> None:
    """Swap real `adapters/*` constructors for `tests/fakes/` doubles at the
    wiring seam — `cli/_wiring.py`'s module-global names, the exact objects
    every `_run_*` function calls at dispatch time (CONTRACTS.md §0's "the
    ONLY module that constructs adapters" boundary). Both `_forge_api`
    (`reconcile`/`classify-pr`/`governance-check`/`governance-poll`, which
    also require the runner's env vars via `_require_env`) and `_project_api`
    (`_run_announce`'s index-side and fork-side clients, which never go
    through `_forge_api` at all — fork-PR announce revamp) are patched, so no
    test here needs a real env var, and neither needs to care which forge the
    sniff would have picked."""
    files_double = files if files is not None else InMemoryFiles()
    github_double = github or FakeGitHub()
    # Every real checkout carries the deployment's registry-host policy, and
    # `announce` reads the index repo's copy over the API — seed both so the
    # `_run_*` functions under test see what production sees (a test asserting
    # the ABSENT-policy failure seeds neither; see `_index_policy` below).
    files_double.write_bytes(INDEX_POLICY_PATH, _POLICY_BYTES)
    github_double.files[(INDEX_POLICY_PATH, "main")] = _POLICY_BYTES

    def _local_files(**_: object) -> InMemoryFiles:
        return files_double

    def _project_api_double(*_: object, **__: object) -> FakeGitHub:
        return github_double

    # `_registry` (the per-host router factory), not the `RegistryV2` class
    # itself: every `_run_*` reaches the registry through it, and a fake needs
    # no host routing — `tests/adapters/test_registry_v2.py` owns the router.
    monkeypatch.setattr(_wiring, "_registry", lambda: registry or FakeRegistry())
    monkeypatch.setattr(_wiring, "_forge_api", lambda: github_double)
    monkeypatch.setattr(_wiring, "_project_api", _project_api_double)
    monkeypatch.setattr(_wiring, "LocalFiles", _local_files)
    monkeypatch.setattr(_wiring, "SystemClock", lambda: clock or FixedClock())


# --- end-to-end happy paths, one per subcommand (exit 0) -----------------------


def test_announce_out_mode_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    tag = "1.0.0"
    committed = _root({})
    registry = FakeRegistry(tags={_REPO: [tag]}, manifests={(_REPO, tag): _index()})
    github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(committed)})
    files = InMemoryFiles()
    _patch_adapters(monkeypatch, registry=registry, github=github, files=files)

    result = main_module.main(
        [
            "announce",
            "--index-repo",
            "ocx-sh/index",
            "--package",
            f"{_NS}/{_PKG}",
            "--tags",
            tag,
            "--out",
            "dist",
        ]
    )

    assert result == ExitCode.OK
    assert files.exists(f"dist/{_ROOT_PATH}")


def test_announce_fork_mode_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "publisher-token")
    tag = "1.0.0"
    committed = _root({})
    registry = FakeRegistry(tags={_REPO: [tag]}, manifests={(_REPO, tag): _index()})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(committed)}, refs={"main": "sha"}
    )
    _patch_adapters(monkeypatch, registry=registry, github=github)

    result = main_module.main(
        [
            "announce",
            "--index-repo",
            "ocx-sh/index",
            "--package",
            f"{_NS}/{_PKG}",
            "--tags",
            tag,
            "--fork",
            "alice/index",
        ]
    )

    assert result == ExitCode.OK


def test_reconcile_empty_index_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_adapters(monkeypatch, files=InMemoryFiles())

    assert main_module.main(["reconcile"]) == ExitCode.OK


def test_validate_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    object_bytes = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + "a" * 64,
                    "size": 512,
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ],
        }
    ).encode()
    digest = f"sha256:{hashlib.sha256(object_bytes).hexdigest()}"
    root = _root({"1.0.0": TagEntry(content=digest, observed="2026-07-17T00:00:00Z")})

    files = InMemoryFiles(
        files={
            _ROOT_PATH: serialize_package_root(root),
            f"p/{_NS}/{_PKG}/o/sha256/{digest.removeprefix('sha256:')}.json": object_bytes,
        }
    )
    _patch_adapters(monkeypatch, files=files)

    assert main_module.main(["validate", _ROOT_PATH, "--offline"]) == ExitCode.OK


def test_validate_base_dir_wires_a_second_file_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--base-dir` gives `validate` the base-ref bytes it needs to tell an
    update to a reserved root from a fresh claim (ADR-2 ND-4). Without the
    wiring, `p/ocx/cli.json` is rejected here even though the identical root
    is on the base ref — the fork re-announce lane's whole failure mode."""
    reserved_path = "p/ocx/cli.json"
    root = PackageRoot(
        name="ocx.sh/ocx/cli",
        repository=_REPO,
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags={},
    )
    files = InMemoryFiles(files={reserved_path: serialize_package_root(root)})
    _patch_adapters(monkeypatch, files=files)

    # `_patch_adapters` hands every `LocalFiles(...)` the same double, so the
    # base-dir port sees the same committed root — an announce-shaped no-op.
    assert (
        main_module.main(["validate", reserved_path, "--offline", "--base-dir", "base"])
        == ExitCode.OK
    )
    assert main_module.main(["validate", reserved_path, "--offline"]) == ExitCode.VALIDATION_FAILURE


def test_render_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    files = InMemoryFiles()
    _patch_adapters(monkeypatch, files=files)

    result = main_module.main(["render", "--index-dir", "", "--out", "dist", "--allow-empty"])

    assert result == ExitCode.OK
    written = files.read_text("dist/config.json")
    assert written is not None
    assert json.loads(written) == {"format_version": 1, "name_segments": 2}


def test_seed_import_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog_md = "\n".join(
        [
            "---",
            "title: CMake",
            "description: A build system",
            "keywords: build, cmake",
            "---",
            "Readme body.",
        ]
    )
    mirror_yml = f"repository: {_REPO}\n"
    files = InMemoryFiles(
        files={"catalog.md": catalog_md.encode("utf-8"), "mirror.yml": mirror_yml.encode("utf-8")}
    )
    registry = FakeRegistry(tags={_REPO: ["1.0.0"]}, manifests={(_REPO, "1.0.0"): _index()})
    _patch_adapters(monkeypatch, files=files, registry=registry)

    result = main_module.main(
        [
            "seed-import",
            "--catalog-md",
            "catalog.md",
            "--mirror-yml",
            "mirror.yml",
            "--namespace",
            _NS,
            "--package",
            _PKG,
            "--owner-github",
            "alice",
            "--owner-github-id",
            "1",
        ]
    )

    assert result == ExitCode.OK
    assert files.exists(_ROOT_PATH)


def test_classify_pr_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    tag_content = _observed_content_digest("1.0.0")
    root = _root({"1.0.0": TagEntry(content=tag_content, observed="2026-07-17T00:00:00Z")})
    info = PullRequestInfo(
        number=1, base_sha="base-sha", head_sha="head-sha", changed_paths=(_ROOT_PATH,)
    )
    github = FakeGitHub(
        files={(_ROOT_PATH, "head-sha"): serialize_package_root(root)},
        pull_request_info={1: info},
    )
    _patch_adapters(monkeypatch, github=github)

    result = main_module.main(["classify-pr", "--pr-number", "1"])

    assert result == ExitCode.OK
    assert github.labels[1] == ["new-package"]


def test_governance_check_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    tag_content = _observed_content_digest("1.0.0")
    committed = _root({"1.0.0": TagEntry(content=tag_content, observed="T0")})
    refreshed = _root({"1.0.0": TagEntry(content=tag_content, observed="T1")})
    info = PullRequestInfo(
        number=1,
        base_sha="base-sha",
        head_sha="head-sha",
        changed_paths=(_ROOT_PATH,),
        author_login=_OWNER.github,
        author_id=_OWNER.github_id,
    )
    github = FakeGitHub(
        files={
            (_ROOT_PATH, "base-sha"): serialize_package_root(committed),
            (_ROOT_PATH, "head-sha"): serialize_package_root(refreshed),
        },
        pull_request_info={1: info},
    )
    _patch_adapters(monkeypatch, github=github)

    result = main_module.main(["governance-check", "--pr-number", "1"])

    assert result == ExitCode.OK
    assert github.statuses["head-sha"] == [
        (
            "governance/review-required",
            "success",
            "refresh: PR author owns every touched package, no review required",
        )
    ]


def test_governance_gate_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring assertion for `governance-gate`: it reaches a real port set
    through the production dispatch, same as `classify-pr`/`governance-check`
    above, and — the one thing those two don't do — also arms auto-merge
    itself on a green disposition."""
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    tag_content = _observed_content_digest("1.0.0")
    committed = _root({"1.0.0": TagEntry(content=tag_content, observed="T0")})
    refreshed = _root({"1.0.0": TagEntry(content=tag_content, observed="T1")})
    info = PullRequestInfo(
        number=1,
        base_sha="base-sha",
        head_sha="head-sha",
        changed_paths=(_ROOT_PATH,),
        author_login=_OWNER.github,
        author_id=_OWNER.github_id,
    )
    github = FakeGitHub(
        files={
            (_ROOT_PATH, "base-sha"): serialize_package_root(committed),
            (_ROOT_PATH, "head-sha"): serialize_package_root(refreshed),
        },
        pull_request_info={1: info},
    )
    _patch_adapters(monkeypatch, github=github)

    result = main_module.main(["governance-gate", "--pr", "1"])

    assert result == ExitCode.OK
    assert github.auto_merge_enabled == {1}
    assert github.auto_merge_head_sha[1] == "head-sha"


def test_governance_gate_arm_only_never_reads_the_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring assertion for `governance.yml`'s `arm-auto-merge` job: the
    shipped entrypoint really reaches `sync_auto_merge` WITHOUT a base-ref
    policy fetch.

    That is not a performance note. The job runs on `if: ${{ !cancelled() }}`
    precisely so a gate that errored still withdraws, so every avoidable
    failure between the runner and the withdraw defeats the point — a policy
    read the gate already choked on would be exactly that failure. The fake
    below serves no policy file at all: an implementation that fetched one
    would raise here instead of withdrawing.
    """
    github = FakeGitHub(pull_request_info={})
    _patch_adapters(monkeypatch, github=github)
    del github.files[(INDEX_POLICY_PATH, "main")]

    result = main_module.main(
        [
            "governance-gate",
            "--pr",
            "1",
            "--arm-only",
            "--disposition",
            "success",
            "--head-sha",
            "head-sha",
        ]
    )

    assert result == ExitCode.OK
    assert github.auto_merge_enabled == {1}
    assert github.auto_merge_head_sha[1] == "head-sha"


def test_governance_gate_arm_only_withdraws_when_the_gate_published_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half, end to end through the CLI: an empty `--disposition` —
    what a failed `governance-gate` job publishes — reaches `withdraw_auto_merge`
    rather than arming or erroring."""
    github = FakeGitHub(pull_request_info={})
    _patch_adapters(monkeypatch, github=github)
    github.enable_auto_merge(1, head_sha="head-sha")

    assert main_module.main(["governance-gate", "--pr", "1", "--arm-only"]) == ExitCode.OK
    assert github.auto_merge_enabled == set(), "an armed PR must be disarmed, not left standing"


def test_governance_poll_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring assertion for the GitLab governance lane: `governance-poll`
    reaches a real port set through the production dispatch, sweeps the open
    MRs it finds and arms the green one."""
    del tmp_path  # the poll lane writes no job output — there is no single disposition
    tag_content = _observed_content_digest("1.0.0")
    committed = _root({"1.0.0": TagEntry(content=tag_content, observed="T0")})
    refreshed = _root({"1.0.0": TagEntry(content=tag_content, observed="T1")})
    info = PullRequestInfo(
        number=4,
        base_sha="base-sha",
        head_sha="head-sha",
        changed_paths=(_ROOT_PATH,),
        author_login=_OWNER.github,
        author_id=_OWNER.github_id,
    )
    github = FakeGitHub(
        files={
            (_ROOT_PATH, "base-sha"): serialize_package_root(committed),
            (_ROOT_PATH, "head-sha"): serialize_package_root(refreshed),
        },
        pull_request_info={4: info},
    )
    _patch_adapters(monkeypatch, github=github)

    assert main_module.main(["governance-poll"]) == ExitCode.OK
    assert github.auto_merge_enabled == {4}


def test_label_failed_run_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring assertion for `label-failed-run`: it reaches a real
    `ForgePort` through the production dispatch and applies the label FP-8
    scopes to fork PRs — coverage of the module alone cannot tell a wired
    subcommand from a dead one."""
    github = FakeGitHub(head_sha_lookup={"deadbeef": PullRequestHeadMatch(number=9, is_fork=True)})
    _patch_adapters(monkeypatch, github=github)

    result = main_module.main(["label-failed-run", "--head-sha", "deadbeef"])

    assert result == ExitCode.OK
    assert github.labels[9] == ["checks-failed"]


def test_stale_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring assertion for `stale`: it reaches a real `ForgePort` and
    `ClockPort` through the production dispatch and marks a long-idle
    checks-failed PR stale."""
    info = PullRequestInfo(
        number=5,
        base_sha="base-sha",
        head_sha="head-sha",
        changed_paths=(),
        updated_at="2026-06-01T00:00:00Z",
        labels=("checks-failed",),
    )
    github = FakeGitHub(pull_request_info={5: info})
    _patch_adapters(monkeypatch, github=github)

    assert main_module.main(["stale"]) == ExitCode.OK
    assert "checks-failed-stale" in github.labels[5]


def test_ci_is_wired_to_a_repo_root_file_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`indexbot ci --check` reaches `cli/ci_cmd.run` with a `LocalFiles` at
    the checkout root and this repo's committed policy — the wiring assertion
    for the generator."""
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "index-policy.json").write_bytes(
        _POLICY_BYTES.replace(
            b"]}", b'], "ci": {"owner": "ocx-sh", "run": "uv run --frozen -- indexbot"}}'
        )
    )

    # Nothing rendered yet, so every generated file is missing: drift.
    assert main_module.main(["ci", "--check"]) == ExitCode.VALIDATION_FAILURE
    assert main_module.main(["ci"]) == ExitCode.OK
    assert (tmp_path / ".github" / "workflows" / "governance.yml").exists()
    assert main_module.main(["ci", "--check"]) == ExitCode.OK


# --- exit-code coverage across the real production dispatch --------------------


def test_validate_missing_path_exits_validation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_adapters(monkeypatch, files=InMemoryFiles())

    result = main_module.main(["validate", "p/does/not-exist.json", "--offline"])

    assert result == ExitCode.VALIDATION_FAILURE


def test_announce_typo_tag_exits_validation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    committed = _root({})
    github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(committed)})
    registry = FakeRegistry()  # no tags/manifests registered at all
    _patch_adapters(monkeypatch, registry=registry, github=github)

    result = main_module.main(
        [
            "announce",
            "--index-repo",
            "ocx-sh/index",
            "--package",
            f"{_NS}/{_PKG}",
            "--tags",
            "9.9.9-typo",
            "--out",
            "dist",
        ]
    )

    assert result == ExitCode.VALIDATION_FAILURE


def test_reconcile_transient_backoff_exhaustion_exits_75(monkeypatch: pytest.MonkeyPatch) -> None:
    tag_content = _observed_content_digest("1.0.0")
    committed = _root({"1.0.0": TagEntry(content=tag_content, observed="2026-07-17T00:00:00Z")})
    files = InMemoryFiles(files={_ROOT_PATH: serialize_package_root(committed)})

    def _raise_transient(repository: str, reference: str) -> object:
        raise TransientError("registry backoff exhausted (test double)")

    registry = FakeRegistry(tags={_REPO: ["1.0.0"]})
    monkeypatch.setattr(registry, "get_manifest", _raise_transient)
    _patch_adapters(monkeypatch, files=files, registry=registry, github=FakeGitHub())

    result = main_module.main(["reconcile"])

    assert result == ExitCode.TRANSIENT


# --- argparse-level surfaces -----------------------------------------------------


def test_help_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main_module.main(["--help"])
    assert exc_info.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_render_requires_index_dir(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main_module.main(["render", "--out", "dist"])
    assert exc_info.value.code == 2
    assert "--index-dir" in capsys.readouterr().err
