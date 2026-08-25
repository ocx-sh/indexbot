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

from ocx_indexbot.adapters.github_api import GitHubApi
from ocx_indexbot.cli.main import main
from ocx_indexbot.core.validate_entry import serialize_package_root
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import Owner, PackageRoot, TagEntry

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.integration.harness.fake_forge import FakeForgeServer

_WIRING_GITHUB = "ocx_indexbot.cli._wiring.GitHubApi"
_REPOSITORY = "ocx-sh/index"
_FORGE_TOKEN = "forge-token-governance"  # noqa: S105
_PR_NUMBER = 1
_BASE_SHA = "base-sha"
_HEAD_SHA = "head-sha"
_ROOT_PATH = "p/kitware/cmake.json"
_ROOT_SEGMENTS = ("contents", "p", "kitware", "cmake.json")
_MAINTAINERS_SEGMENTS = ("contents", ".github", "maintainers.yml")
_MAINTAINERS_YML = b"maintainers:\n  - login: carol\n    id: 99\n"
_OWNER = Owner(login="alice", id=1)
_OTHER_OWNER = Owner(login="bob", id=2)


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


_POLICY_SEGMENTS = ("contents", ".github", "index-policy.json")
_POLICY_BYTES = (
    b'{"name": "ocx.sh", "name_segments": 2, "registry_hosts": ["ghcr.io"], '
    b'"reserved_namespaces": ["ocx", "ocx-sh", "ocx-contrib", "ocx-rs"]}\n'
)


def _stub_policy(forge: FakeForgeServer) -> None:
    """Serve `.github/index-policy.json` at `main`.

    The privileged subcommands read the deployment's identity there, over the
    API and from the BASE ref — they never check the repository out, and must
    not take the PR head's copy: a fork that could declare its own
    `name_segments` would be choosing how its own diff is classified.
    """
    encoded = base64.b64encode(_POLICY_BYTES).decode("ascii")
    forge.stub_json(
        "GET",
        forge.repo_path(*_POLICY_SEGMENTS),
        {"content": encoded, "encoding": "base64"},
        params={"ref": "main"},
    )


def _approval(login: str, uid: int, commit_id: str) -> dict[str, object]:
    """One GitHub review payload. Both identity fields are present because the
    adapter must take the numeric one — see
    `test_a_recycled_maintainer_login_does_not_release_the_human_lane`."""
    return {"user": {"login": login, "id": uid}, "state": "APPROVED", "commit_id": commit_id}


def _pr_payload(*, login: str, uid: int) -> dict[str, object]:
    return {
        "base": {"sha": _BASE_SHA},
        "head": {"sha": _HEAD_SHA},
        "user": {"login": login, "id": uid},
        "updated_at": "2026-07-17T00:00:00Z",
        "labels": [],
    }


_SELF_ID = 1001
_USER_PATH = "/user"


def _setup_forge(
    forge: FakeForgeServer,
    *,
    login: str,
    uid: int,
    base_root: PackageRoot | None,
    head_root: PackageRoot,
    approvals: list[dict[str, object]] | None = None,
) -> None:
    _stub_policy(forge)
    # `GET /user` — the token identity `create_comment` matches a marked
    # comment against. A PAT answers it; the installation token a workflow's
    # `GITHUB_TOKEN` is answers 403, which the adapter's own suite covers.
    forge.stub_json("GET", _USER_PATH, {"id": _SELF_ID, "login": "indexbot"})
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
    forge.stub_json("GET", forge.repo_path("pulls", str(_PR_NUMBER), "reviews"), approvals or [])
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
    """Every request stays on this repository's own API surface.

    `/user` is the one deliberate exception: it is the token asking who it
    is, carries no repository scope, and is what keeps `create_comment` from
    adopting a comment somebody else wrote (`adapters/github_api.
    _is_repo_side_author`). Named explicitly rather than prefix-matched, so a
    new off-repo path still has to be argued for here.
    """
    expected_prefix = forge.repo_path()
    for _method, path in forge.requests:
        assert path == _USER_PATH or path.startswith(expected_prefix), path


def _run(
    monkeypatch: pytest.MonkeyPatch,
    forge: FakeForgeServer,
    tmp_path: Path,
    *,
    login: str,
    uid: int,
    base_root: PackageRoot | None,
    head_root: PackageRoot,
    approvals: list[dict[str, object]] | None = None,
) -> str:
    output_file = tmp_path / "github_output.txt"
    _setup_env(monkeypatch, forge, output_file)
    _setup_forge(
        forge,
        login=login,
        uid=uid,
        base_root=base_root,
        head_root=head_root,
        approvals=approvals,
    )

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
        login=_OWNER.login,
        uid=_OWNER.id,
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
        login=_OWNER.login,
        uid=_OWNER.id,
        base_root=None,
        head_root=head,
    )

    assert "pending" in output
    assert _reviewers_requested(fake_forge)
    assert _comment_posted(fake_forge)


def test_a_maintainers_approval_releases_the_human_lane(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The human lane's only exit, and the reason it has to exist: on a forge
    where the commit status IS the merge gate, a `pending` nobody can turn
    green is not a stalled merge request but a permanently unmergeable one.

    `carol` is the committed maintainer; `alice` opened the PR. The approval
    is recorded against this PR's current head, which is what makes it an
    approval of *this* revision rather than of something that used to be here.
    """
    output = _run(
        monkeypatch,
        fake_forge,
        tmp_path,
        login=_OWNER.login,
        uid=_OWNER.id,
        base_root=None,
        head_root=_root(tags={}),
        approvals=[_approval("carol", 99, _HEAD_SHA)],
    )

    assert "success" in output


def test_a_stale_approval_does_not_release_the_human_lane(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GitHub records the commit a review was left on, so an approval of an
    earlier push is not an approval of what would merge now. The whole point
    of the release is that a person looked at *these* bytes."""
    output = _run(
        monkeypatch,
        fake_forge,
        tmp_path,
        login=_OWNER.login,
        uid=_OWNER.id,
        base_root=None,
        head_root=_root(tags={}),
        approvals=[_approval("carol", 99, "older-push")],
    )

    assert "pending" in output


def test_a_recycled_maintainer_login_does_not_release_the_human_lane(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The committed maintainer is `carol`, `github_id: 99`. The approval below
    is left by an account that *calls itself* `carol` and is somebody else —
    which is what a GitHub login rename plus a re-registration of the freed
    name produces, with no action by anyone on this repository.

    Matching the approval by login would release the human lane for a
    stranger, and an approval outranks every disposition including
    `governance.auto_merge = never`. Matching by `github_id` — the same field
    `owners[]` binds on for G-19 — leaves the lane exactly where it was.
    """
    output = _run(
        monkeypatch,
        fake_forge,
        tmp_path,
        login=_OWNER.login,
        uid=_OWNER.id,
        base_root=None,
        head_root=_root(tags={}),
        approvals=[_approval("carol", 4242, _HEAD_SHA)],
    )

    assert "pending" in output


def test_an_approval_from_outside_the_maintainer_list_is_not_a_review(
    fake_forge: FakeForgeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`carol` is the only committed maintainer. Anyone else approving —
    the author included, which is the case that would make the human lane a
    formality — leaves the lane exactly where it was."""
    output = _run(
        monkeypatch,
        fake_forge,
        tmp_path,
        login=_OWNER.login,
        uid=_OWNER.id,
        base_root=None,
        head_root=_root(tags={}),
        approvals=[_approval(_OWNER.login, _OWNER.id, _HEAD_SHA)],
    )

    assert "pending" in output
