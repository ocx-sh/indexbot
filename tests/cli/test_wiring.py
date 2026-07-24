"""`cli/_wiring.py` — production DI-construction unit tests, plus end-to-end
`cli/main.main()` tests that swap real `adapters/*` for `tests/fakes/` at the
wiring seam (monkeypatching the adapter-constructor names `cli/_wiring.py`
calls, never `main.main`'s own `_DISPATCH` — that seam is `test_main.py`'s,
this file exercises the real production dispatch table end to end).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from indexbot.cli import _wiring
from indexbot.cli import main as main_module
from indexbot.core.observe import observe
from indexbot.core.validate_entry import serialize_observation_object, serialize_package_root
from indexbot.errors import TransientError
from indexbot.exit_codes import ExitCode
from indexbot.model import (
    ObservationObject,
    OciPlatform,
    Owner,
    PackageRoot,
    PlatformEntry,
    PullRequestInfo,
    TagEntry,
)
from tests.fakes import FakeGitHub, FakeRegistry, FixedClock, InMemoryFiles

_NS = "kitware"
_PKG = "cmake"
_REPO = "oci://ghcr.io/kitware/cmake"
_ROOT_PATH = f"p/{_NS}/{_PKG}.json"
_OWNER = Owner(github="alice", github_id=1)


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


# --- `_github_api` ---------------------------------------------------------------


def test_github_api_reads_owner_repo_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    api = _wiring._github_api()  # pyright: ignore[reportPrivateUsage]
    assert api.owner == "ocx-sh"
    assert api.repo == "index"
    assert api.token == "secret-token"  # noqa: S105 - test fixture, not a real credential


def test_github_api_missing_repository_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    with pytest.raises(RuntimeError, match="GITHUB_REPOSITORY"):
        _wiring._github_api()  # pyright: ignore[reportPrivateUsage]


def test_github_api_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "ocx-sh/index")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        _wiring._github_api()  # pyright: ignore[reportPrivateUsage]


# --- DISPATCH table shape -------------------------------------------------------


def test_dispatch_registers_exactly_the_seven_subcommands() -> None:
    assert set(_wiring.DISPATCH) == {
        "announce",
        "reconcile",
        "validate",
        "render",
        "seed-import",
        "classify-pr",
        "governance-check",
    }


def test_main_dispatch_is_seeded_from_wiring_dispatch() -> None:
    assert set(main_module._DISPATCH) == set(_wiring.DISPATCH)  # pyright: ignore[reportPrivateUsage]


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


def _manifest() -> dict[str, object]:
    return {"platform": {"architecture": "amd64", "os": "linux"}}


def _observed_content_digest(tag: str) -> str:
    """The exact `Observation.content_digest` `observe()` computes for a
    single-tag, single-platform manifest — used to seed a committed root's
    `TagEntry.content` so a later `observe()` call over the same fake
    registry state reproduces byte-identical output (a genuine no-op diff),
    matching `tests/cli/test_announce.py`'s established fixture pattern.
    """
    registry = FakeRegistry(tags={_REPO: [tag]}, manifests={(_REPO, tag): _manifest()})
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
    ONLY module that constructs adapters" boundary). Both `_github_api`
    (`reconcile`/`classify-pr`/`governance-check`, which also require
    `GITHUB_REPOSITORY`/`GITHUB_TOKEN` env presence via `_require_env`) and
    the bare `GitHubApi` class (`_run_announce`'s `_index_github`/fork-mode
    construction, which never goes through `_github_api` at all — fork-PR
    announce revamp) are patched, so no test here needs a real env var."""
    files_double = files if files is not None else InMemoryFiles()
    github_double = github or FakeGitHub()

    def _local_files(**_: object) -> InMemoryFiles:
        return files_double

    def _github_api_double(**_: object) -> FakeGitHub:
        return github_double

    monkeypatch.setattr(_wiring, "GhcrRegistry", lambda: registry or FakeRegistry())
    monkeypatch.setattr(_wiring, "_github_api", lambda: github_double)
    monkeypatch.setattr(_wiring, "GitHubApi", _github_api_double)
    monkeypatch.setattr(_wiring, "LocalFiles", _local_files)
    monkeypatch.setattr(_wiring, "SystemClock", lambda: clock or FixedClock())


# --- end-to-end happy paths, one per subcommand (exit 0) -----------------------


def test_announce_out_mode_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    tag = "1.0.0"
    committed = _root({})
    registry = FakeRegistry(tags={_REPO: [tag]}, manifests={(_REPO, tag): _manifest()})
    github = FakeGitHub(files={(_ROOT_PATH, "main"): serialize_package_root(committed)})
    files = InMemoryFiles()
    _patch_adapters(monkeypatch, registry=registry, github=github, files=files)

    result = main_module.main(
        ["announce", "--package", f"{_NS}/{_PKG}", "--tags", tag, "--out", "dist"]
    )

    assert result == ExitCode.OK
    assert files.exists(f"dist/{_ROOT_PATH}")


def test_announce_fork_mode_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "publisher-token")
    tag = "1.0.0"
    committed = _root({})
    registry = FakeRegistry(tags={_REPO: [tag]}, manifests={(_REPO, tag): _manifest()})
    github = FakeGitHub(
        files={(_ROOT_PATH, "main"): serialize_package_root(committed)}, refs={"main": "sha"}
    )
    _patch_adapters(monkeypatch, registry=registry, github=github)

    result = main_module.main(
        [
            "announce",
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
    platform = OciPlatform(architecture="amd64", os="linux")
    obj = ObservationObject(
        platforms=(PlatformEntry(platform=platform, digest="sha256:" + "a" * 64),)
    )
    object_bytes = serialize_observation_object(obj)
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


def test_render_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    files = InMemoryFiles()
    _patch_adapters(monkeypatch, files=files)

    result = main_module.main(["render", "--index-dir", "", "--out", "dist"])

    assert result == ExitCode.OK
    written = files.read_text("dist/config.json")
    assert written is not None
    assert json.loads(written) == {"format_version": 1}


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
    registry = FakeRegistry(tags={_REPO: ["1.0.0"]}, manifests={(_REPO, "1.0.0"): _manifest()})
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
        ["announce", "--package", f"{_NS}/{_PKG}", "--tags", "9.9.9-typo", "--out", "dist"]
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
