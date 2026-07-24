"""End-to-end `indexbot governance-check` through `main()` against the
socket-level `FakeForgeServer` (WP-B4, plan Phase 5).

Drives the REAL `GitHubApi` adapter through the full G-19/G-20 disposition:

- An owner-authored refresh (PR author's `github_id` in `owners[]` of every
  touched root, read from the base ref) sets the `governance/review-required`
  status `success` — the auto-merge-armed machine lane — and assigns no
  reviewers, posts no comment.
- A non-owner refresh falls back to the human lane: `pending`, reviewers
  assigned from committed `.github/maintainers.yml`, one idempotent comment.
- A brand-new package is always the human lane: `pending` + reviewers +
  comment.

The disposition is read back from the `$GITHUB_OUTPUT` `disposition` entry the
run writes; the reviewer-request and comment POSTs are confirmed present (human
lane) or absent (machine lane) from the fake's request log. Every request is
confirmed to target this server's own `owner/repo` surface (X6: this forge-only
flow carries no registry credential, so the path-level check is the relevant
leak assertion here; the forge fake's `received_headers` backs the bidirectional
proof in the reconcile flow)."""

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
_FORGE_TOKEN = "forge-token-governance"  # noqa: S105
_PR_NUMBER = 1
_BASE_SHA = "base-sha"
_HEAD_SHA = "head-sha"
_ROOT_PATH = "p/kitware/cmake.json"
_ROOT_SEGMENTS = ("contents", "p", "kitware", "cmake.json")
_MAINTAINERS_SEGMENTS = ("contents", ".github", "maintainers.yml")
_MAINTAINERS_YML = b"maintainers:\n  - github: carol\n    github_id: 99\n"
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


def _contents(raw: bytes) -> dict[str, str]:
    return {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}


def _pr_payload(*, login: str, uid: int) -> dict[str, object]:
    return {
        "base": {"sha": _BASE_SHA},
        "head": {"sha": _HEAD_SHA},
        "user": {"login": login, "id": uid},
    }


def _setup_forge(
    forge: FakeForgeServer,
    *,
    login: str,
    uid: int,
    base_root: PackageRoot | None,
    head_root: PackageRoot,
) -> None:
    forge.stub_json(
        "GET", forge.repo_path("pulls", str(_PR_NUMBER)), _pr_payload(login=login, uid=uid)
    )
    forge.stub_json(
        "GET", forge.repo_path("pulls", str(_PR_NUMBER), "files"), [{"filename": _ROOT_PATH}]
    )
    if base_root is not None:
        forge.stub_json(
            "GET",
            forge.repo_path(*_ROOT_SEGMENTS),
            _contents(serialize_package_root(base_root)),
            params={"ref": _BASE_SHA},
        )
    forge.stub_json(
        "GET",
        forge.repo_path(*_ROOT_SEGMENTS),
        _contents(serialize_package_root(head_root)),
        params={"ref": _HEAD_SHA},
    )
    forge.stub_json("POST", forge.repo_path("statuses", _HEAD_SHA), {}, status=200)
    forge.stub_json(
        "GET",
        forge.repo_path(*_MAINTAINERS_SEGMENTS),
        _contents(_MAINTAINERS_YML),
        params={"ref": _BASE_SHA},
    )
    forge.stub_json(
        "POST", forge.repo_path("pulls", str(_PR_NUMBER), "requested_reviewers"), {}, status=200
    )
    forge.stub_json("GET", forge.repo_path("issues", str(_PR_NUMBER), "comments"), [])
    forge.stub_json(
        "POST", forge.repo_path("issues", str(_PR_NUMBER), "comments"), {"id": 10}, status=201
    )


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


def _assert_only_repo_paths(forge: FakeForgeServer) -> None:
    expected_prefix = forge.repo_path()
    for _method, path in forge.requests:
        assert path.startswith(expected_prefix), path


def _run(
    monkeypatch: pytest.MonkeyPatch,
    forge: FakeForgeServer,
    tmp_path: Path,
    *,
    login: str,
    uid: int,
    base_root: PackageRoot | None,
    head_root: PackageRoot,
) -> str:
    output_file = tmp_path / "github_output.txt"
    _setup_env(monkeypatch, forge, output_file)
    _setup_forge(forge, login=login, uid=uid, base_root=base_root, head_root=head_root)

    exit_code = main(["governance-check", "--pr-number", str(_PR_NUMBER)])

    assert exit_code == ExitCode.OK
    assert ("POST", forge.repo_path("statuses", _HEAD_SHA)) in forge.requests
    _assert_only_repo_paths(forge)
    return output_file.read_text(encoding="utf-8")


def _reviewers_requested(forge: FakeForgeServer) -> bool:
    path = forge.repo_path("pulls", str(_PR_NUMBER), "requested_reviewers")
    return ("POST", path) in forge.requests


def _comment_posted(forge: FakeForgeServer) -> bool:
    path = forge.repo_path("issues", str(_PR_NUMBER), "comments")
    return ("POST", path) in forge.requests


def test_owner_refresh_arms_auto_merge_with_success_status(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    output = _run(
        monkeypatch,
        fake_forge,
        tmp_path,
        login=_OWNER.github,
        uid=_OWNER.github_id,
        base_root=before,
        head_root=after,
    )

    assert "success" in output
    # Machine lane: no human review requested, no comment posted.
    assert not _reviewers_requested(fake_forge)
    assert not _comment_posted(fake_forge)


def test_non_owner_refresh_falls_back_to_pending_with_reviewers_and_comment(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    output = _run(
        monkeypatch,
        fake_forge,
        tmp_path,
        login="mallory",
        uid=999,
        base_root=before,
        head_root=after,
    )

    assert "pending" in output
    assert _reviewers_requested(fake_forge)
    assert _comment_posted(fake_forge)


def test_new_package_is_pending_human_lane(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    head = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    output = _run(
        monkeypatch,
        fake_forge,
        tmp_path,
        login=_OWNER.github,
        uid=_OWNER.github_id,
        base_root=None,
        head_root=head,
    )

    assert "pending" in output
    assert _reviewers_requested(fake_forge)
    assert _comment_posted(fake_forge)
