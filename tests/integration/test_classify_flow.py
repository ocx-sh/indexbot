"""End-to-end `indexbot classify-pr` through `main()` against the socket-level
`FakeForgeServer` (WP-B4, plan Phase 5).

Drives the REAL `GitHubApi` adapter: fake-forge serves a PR's REST surface (the
`pulls/<n>` payload, the changed-file list, and each root's base/head tree bytes
via the Contents API) over a real socket, and `classify_pr.run` labels the PR
across the lane matrix — a brand-new root (`new-package`), a tag-only content
change (`refresh`), and an `owners[]` edit (`human-review-required`).

The classification the run computed is read back from the `$GITHUB_OUTPUT`
`classification` entry the run writes; the label POST that carried the same
value is confirmed to have hit the fake. Every forge request is confirmed to
target the expected `/repos/<owner>/<repo>/...` surface (X6: no request escaped
to an unexpected path. This forge-only flow carries no registry credential, so
the path-level assertion is the relevant leak check here; the forge fake's
`received_headers` capture backs the bidirectional token-leak proof in the
reconcile flow)."""

from __future__ import annotations

import base64
import functools
from typing import TYPE_CHECKING

from indexbot.adapters.github_api import GitHubApi
from indexbot.cli.main import main
from indexbot.core.validate_entry import serialize_package_root
from indexbot.exit_codes import ExitCode
from indexbot.model import Owner, PackageRoot, TagEntry

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.integration.harness.fake_forge import FakeForgeServer

_WIRING_GITHUB = "indexbot.cli._wiring.GitHubApi"
_REPOSITORY = "ocx-sh/index"
_FORGE_TOKEN = "forge-token-classify"  # noqa: S105
_PR_NUMBER = 1
_BASE_SHA = "base-sha"
_HEAD_SHA = "head-sha"
_ROOT_PATH = "p/kitware/cmake.json"
_OWN_CAS_PATH = f"p/kitware/cmake/o/sha256/{'b' * 64}.json"
_ROOT_SEGMENTS = ("contents", "p", "kitware", "cmake.json")
_OWNER = Owner(github="alice", github_id=1)
_OTHER_OWNER = Owner(github="bob", github_id=2)


def _root(*, owners: tuple[Owner, ...] = (_OWNER,), tags: dict[str, TagEntry]) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/kitware/cmake",
        owners=owners,
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags),
    )


def _contents(root: PackageRoot) -> dict[str, str]:
    encoded = base64.b64encode(serialize_package_root(root)).decode("ascii")
    return {"content": encoded, "encoding": "base64"}


def _pr_payload() -> dict[str, object]:
    return {
        "base": {"sha": _BASE_SHA},
        "head": {"sha": _HEAD_SHA},
        "user": {"login": _OWNER.github, "id": _OWNER.github_id},
    }


def _setup_env(monkeypatch: pytest.MonkeyPatch, forge: FakeForgeServer, output_file: Path) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", _REPOSITORY)
    monkeypatch.setenv("GITHUB_TOKEN", _FORGE_TOKEN)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(
        _WIRING_GITHUB,
        functools.partial(
            GitHubApi, base_url=forge.base_url, graphql_url=f"{forge.base_url}/graphql"
        ),
    )


def _stub_pr(
    forge: FakeForgeServer,
    *,
    base_root: PackageRoot | None,
    head_root: PackageRoot,
    changed_paths: tuple[str, ...],
) -> None:
    forge.stub_json("GET", forge.repo_path("pulls", str(_PR_NUMBER)), _pr_payload())
    forge.stub_json(
        "GET",
        forge.repo_path("pulls", str(_PR_NUMBER), "files"),
        [{"filename": path} for path in changed_paths],
    )
    if base_root is not None:
        forge.stub_json(
            "GET", forge.repo_path(*_ROOT_SEGMENTS), _contents(base_root), params={"ref": _BASE_SHA}
        )
    forge.stub_json(
        "GET", forge.repo_path(*_ROOT_SEGMENTS), _contents(head_root), params={"ref": _HEAD_SHA}
    )
    forge.stub_json("POST", forge.repo_path("issues", str(_PR_NUMBER), "labels"), [], status=200)


def _assert_only_repo_paths(forge: FakeForgeServer) -> None:
    """X6 forge-side leak seam: every request the fake received targeted this
    server's own `owner/repo` surface — nothing escaped to an unexpected path."""
    expected_prefix = forge.repo_path()
    for _method, path in forge.requests:
        assert path.startswith(expected_prefix), path


def _classify(
    monkeypatch: pytest.MonkeyPatch,
    forge: FakeForgeServer,
    tmp_path: Path,
    *,
    base_root: PackageRoot | None,
    head_root: PackageRoot,
    changed_paths: tuple[str, ...] = (_ROOT_PATH,),
) -> str:
    output_file = tmp_path / "github_output.txt"
    _setup_env(monkeypatch, forge, output_file)
    _stub_pr(forge, base_root=base_root, head_root=head_root, changed_paths=changed_paths)

    exit_code = main(["classify-pr", "--pr-number", str(_PR_NUMBER)])

    assert exit_code == ExitCode.OK
    labels_path = forge.repo_path("issues", str(_PR_NUMBER), "labels")
    assert ("POST", labels_path) in forge.requests
    _assert_only_repo_paths(forge)
    return output_file.read_text(encoding="utf-8")


def test_new_package_lane(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    head = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    output = _classify(monkeypatch, fake_forge, tmp_path, base_root=None, head_root=head)
    assert "new-package" in output


def test_refresh_lane(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The machine lane over a realistic announce diff: the root *plus* the
    observation object it now points at, which is what `indexbot announce`
    actually commits — a single-path diff would not exercise the refresh-scope
    allowlist at all."""
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    output = _classify(
        monkeypatch,
        fake_forge,
        tmp_path,
        base_root=before,
        head_root=after,
        changed_paths=(_ROOT_PATH, _OWN_CAS_PATH),
    )
    assert "refresh" in output


def test_out_of_scope_path_forces_human_lane(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-6 FP-5 end-to-end: the same refresh-shaped root change, carrying one
    workflow edit alongside it, must NOT classify `refresh` — otherwise
    `governance-check` greens it and `validate.yml` auto-merges the workflow
    edit. Fails if the refresh-scope allowlist is removed."""
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    output = _classify(
        monkeypatch,
        fake_forge,
        tmp_path,
        base_root=before,
        head_root=after,
        changed_paths=(_ROOT_PATH, ".github/workflows/validate.yml"),
    )
    assert "human-review-required" in output


def test_human_review_required_lane_on_owners_touched(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tags = {"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")}
    before = _root(owners=(_OWNER,), tags=tags)
    after = _root(owners=(_OTHER_OWNER,), tags=tags)
    output = _classify(monkeypatch, fake_forge, tmp_path, base_root=before, head_root=after)
    assert "human-review-required" in output
