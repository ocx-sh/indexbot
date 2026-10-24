"""`owners[]` is the G-19 ownership key, and 0.5.0 renamed its fields.

`adr_forge_neutral_owners.md` D1 renamed `owners[].github`/`github_id` to
`login`/`id` because the old names claimed a forge the index may not be
hosted on: a gitlab.com index wrote a *GitLab* username under a key spelling
`github`. D2 kept the pre-0.5.0 pair readable and made the emitted copy
derived.

That rename is only safe if the machine (auto-merge) lane still resolves
ownership afterwards, on **both** forges — which is what this module pins.
The forge adapters are exercised for real (respx over the actual
`GitHubApi`/`GitLabApi` HTTP shapes, not a `ForgePort` double) because the
whole point of the rename is that two different forges' user ids land in one
field, and a fake would prove that for neither.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from ocx_indexbot.adapters.github_api import GitHubApi
from ocx_indexbot.adapters.gitlab_api import GitLabApi
from ocx_indexbot.cli.governance_check import (
    _author_owns_every_touched_package,  # pyright: ignore[reportPrivateUsage]
)
from ocx_indexbot.core.validate_entry import parse_package_root, serialize_package_root
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.model import Owner, PackageRoot
from tests.fakes import make_policy

if TYPE_CHECKING:
    from ocx_indexbot.ports import ForgePort

_ROOT_PATH = "p/ns/pkg.json"
_AUTHOR_ID = 4242
_GITHUB_API = "https://api.github.com/repos/ocx-sh/index"
_GITLAB_PROJECT = "https://gitlab.com/api/v4/projects/42"


def _root(*, owner_id: int, spelling: str) -> bytes:
    """One package root's wire bytes, written in the requested `owners[]`
    spelling — `"new"` (the canonical `login`/`id`, what this bot emits since
    0.6.2), `"legacy"` (the pre-0.5.0 `github`/`github_id`, what every index
    published before 0.5.0 carries) or `"both"` (what 0.5.0 through 0.6.1 emitted, and
    what every root written in that window still carries on disk).

    All three must reach the same G-19 verdict: the ownership key is the
    numeric id, whichever spelling carries it.
    """
    root = PackageRoot(
        name="ocx.sh/ns/pkg",
        repository="oci://ghcr.io/ocx-contrib/pkg",
        owners=(Owner(login="alice", id=owner_id),),
        status="active",
        deprecated_message=None,
        created="2026-08-25",
        desc=None,
        tags={},
    )
    payload: dict[str, Any] = json.loads(serialize_package_root(root))
    if spelling == "legacy":
        payload["owners"] = [{"github": "alice", "github_id": owner_id}]
    elif spelling == "new":
        payload["owners"] = [{"login": "alice", "id": owner_id}]
    elif spelling == "both":
        payload["owners"] = [
            {"login": "alice", "id": owner_id, "github": "alice", "github_id": owner_id}
        ]
    return json.dumps(payload, indent=2).encode("utf-8") + b"\n"


def _owns(github: ForgePort, *, pr_number: int) -> bool:
    info = github.get_pull_request_info(pr_number)
    return _author_owns_every_touched_package(info, github, policy=make_policy())


# --- GitHub ----------------------------------------------------------------


def _mock_github(root_bytes: bytes, *, activity: list[dict[str, object]] | None = None) -> None:
    respx.get(f"{_GITHUB_API}/pulls/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "base": {"sha": "base-sha", "repo": {"id": 5}},
                "head": {"sha": "head-sha", "repo": {"id": 5}, "ref": "announce-branch"},
                "user": {"login": "alice", "id": _AUTHOR_ID},
                "created_at": "2026-08-25T00:00:00Z",
                "labels": [],
                "updated_at": "2026-08-25T00:00:00Z",
            },
        )
    )
    respx.get(f"{_GITHUB_API}/pulls/7/files").mock(
        return_value=httpx.Response(200, json=[{"filename": _ROOT_PATH, "status": "modified"}])
    )
    commit_account = {"login": "alice", "id": _AUTHOR_ID}
    respx.get(f"{_GITHUB_API}/pulls/7/commits").mock(
        return_value=httpx.Response(
            200, json=[{"sha": "head-sha", "author": commit_account, "committer": commit_account}]
        )
    )
    respx.get(f"{_GITHUB_API}/issues/7/timeline").mock(return_value=httpx.Response(200, json=[]))
    owners_push = {
        "activity_type": "push",
        "timestamp": "2026-08-25T01:00:00Z",
        "after": "head-sha",
        "actor": commit_account,
    }
    respx.get(f"{_GITHUB_API}/activity").mock(
        return_value=httpx.Response(200, json=[owners_push] if activity is None else activity)
    )
    respx.get(f"{_GITHUB_API}/contents/{_ROOT_PATH}", params={"ref": "base-sha"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": base64.b64encode(root_bytes).decode("ascii"),
                "encoding": "base64",
            },
        )
    )


@pytest.mark.parametrize("spelling", ["new", "legacy", "both"])
@respx.mock
def test_the_auto_merge_lane_resolves_ownership_on_github(spelling: str) -> None:
    """G-19 on GitHub, through the real `GitHubApi`: the numeric id the PR
    payload carries has to match `owners[]` regardless of which spelling the
    committed root uses."""
    _mock_github(_root(owner_id=_AUTHOR_ID, spelling=spelling))

    assert _owns(GitHubApi(owner="ocx-sh", repo="index", token=""), pr_number=7) is True


@respx.mock
def test_a_github_author_who_is_not_an_owner_stays_out_of_the_machine_lane() -> None:
    """The falsifying half — without it the assertions above would pass on a
    predicate that returned `True` unconditionally."""
    _mock_github(_root(owner_id=_AUTHOR_ID + 1, spelling="both"))

    assert _owns(GitHubApi(owner="ocx-sh", repo="index", token=""), pr_number=7) is False


# --- GitLab ----------------------------------------------------------------


def _mock_gitlab(root_bytes: bytes, *, extra_events: list[dict[str, object]] | None = None) -> None:
    respx.get(f"{_GITLAB_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "diff_refs": {"base_sha": "base-sha", "head_sha": "head-sha"},
                "source_project_id": 42,
                "target_project_id": 42,
                "source_branch": "indexbot-announce-ns-pkg",
                "created_at": "2026-08-25T00:00:00.000Z",
                "author": {"username": "alice", "id": _AUTHOR_ID},
                "updated_at": "2026-08-25T00:00:00Z",
                "labels": [],
            },
        )
    )
    respx.get(f"{_GITLAB_PROJECT}/merge_requests/7/diffs").mock(
        return_value=httpx.Response(200, json=[{"new_path": _ROOT_PATH, "deleted_file": False}])
    )
    respx.get(f"{_GITLAB_PROJECT}/merge_requests/7/commits").mock(
        return_value=httpx.Response(200, json=[{"id": "head-sha"}])
    )
    respx.get(f"{_GITLAB_PROJECT}/events", params={"action": "pushed"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "author_id": _AUTHOR_ID,
                    "created_at": "2026-08-25T01:00:00.000Z",
                    "push_data": {"ref": "indexbot-announce-ns-pkg", "commit_to": "head-sha"},
                },
                *(extra_events or []),
            ],
        )
    )
    respx.get(f"{_GITLAB_PROJECT}/repository/files/p%2Fns%2Fpkg.json/raw").mock(
        return_value=httpx.Response(200, content=root_bytes)
    )


@respx.mock
def test_a_github_push_under_the_owners_commit_email_is_still_the_pushers() -> None:
    """The commit list names only the owner, because the pusher wrote the
    owner's email into the commit. GitHub's own push record names who pushed."""
    _mock_github(
        _root(owner_id=_AUTHOR_ID, spelling="new"),
        activity=[
            {
                "activity_type": "push",
                "timestamp": "2026-08-25T01:00:00Z",
                "after": "head-sha",
                "actor": {"login": "mallory", "id": _AUTHOR_ID + 1},
            }
        ],
    )

    assert _owns(GitHubApi(owner="ocx-sh", repo="index", token=""), pr_number=7) is False


@pytest.mark.parametrize("spelling", ["new", "legacy", "both"])
@respx.mock
def test_the_auto_merge_lane_resolves_ownership_on_gitlab(spelling: str) -> None:
    """The same gate on GitLab, where `owners[].login` holds a GitLab username
    and `owners[].id` a GitLab user id — the mismatch between the field name
    and the value that motivated the rename."""
    _mock_gitlab(_root(owner_id=_AUTHOR_ID, spelling=spelling))

    assert _owns(GitLabApi(project="42", token=""), pr_number=7) is True


@respx.mock
def test_a_gitlab_author_who_is_not_an_owner_stays_out_of_the_machine_lane() -> None:
    _mock_gitlab(_root(owner_id=_AUTHOR_ID + 1, spelling="both"))

    assert _owns(GitLabApi(project="42", token=""), pr_number=7) is False


@respx.mock
def test_a_gitlab_push_made_before_the_owner_opened_the_mr_is_still_the_pushers() -> None:
    """Mallory pushes the announce branch, then the owner opens the merge
    request: the event predates `created_at` but its tip is in the merge
    request, so it is counted."""
    _mock_gitlab(
        _root(owner_id=_AUTHOR_ID, spelling="new"),
        extra_events=[
            {
                "author_id": _AUTHOR_ID + 1,
                "created_at": "2026-08-24T23:00:00.000Z",
                "push_data": {"ref": "indexbot-announce-ns-pkg", "commit_to": "head-sha"},
            }
        ],
    )

    assert _owns(GitLabApi(project="42", token=""), pr_number=7) is False


# --- the two spellings may not disagree ------------------------------------


def test_a_root_whose_two_spellings_disagree_is_refused() -> None:
    """The reason the emitted legacy pair is derived rather than stored: a
    hand-authored root could otherwise show one identity to a human reviewer
    and hand a different one to this gate.
    """
    payload = json.loads(_root(owner_id=_AUTHOR_ID, spelling="both"))
    payload["owners"] = [
        {"login": "alice", "id": _AUTHOR_ID, "github": "mallory", "github_id": 999}
    ]
    raw = json.dumps(payload, indent=2).encode("utf-8") + b"\n"

    with pytest.raises(ValidationError, match="carries both spellings and they disagree"):
        parse_package_root(raw)


def test_a_root_whose_ids_disagree_is_refused() -> None:
    """The id half specifically — the login half agreeing is not enough, since
    the id is what G-19 matches on."""
    payload = json.loads(_root(owner_id=_AUTHOR_ID, spelling="both"))
    payload["owners"] = [{"login": "alice", "id": _AUTHOR_ID, "github": "alice", "github_id": 999}]
    raw = json.dumps(payload, indent=2).encode("utf-8") + b"\n"

    with pytest.raises(ValidationError, match="carries both spellings and they disagree"):
        parse_package_root(raw)


def test_the_serializer_emits_the_canonical_pair_only() -> None:
    """0.6.2's wire shape: two keys per owner. `ocx package claim` is the other
    writer of these exact bytes and renders `login`/`id`, so emitting the
    derived legacy pair beside them failed the byte-exact discipline on every
    fresh claim (ocx `adr_index_claim_command.md` W-B, superseding
    `adr_forge_neutral_owners.md` D2's emit-both).

    The read side is untouched — the tests above still pass a legacy-only root
    through this same gate.
    """
    root = parse_package_root(_root(owner_id=_AUTHOR_ID, spelling="new"))

    emitted = json.loads(serialize_package_root(root))

    assert emitted["owners"] == [{"login": "alice", "id": _AUTHOR_ID}]


def test_a_legacy_only_root_re_serializes_to_the_canonical_pair() -> None:
    """The migration path: nothing rewrites a published root, but the first
    write that touches one normalizes it — and the id G-19 matches on survives
    that normalization unchanged."""
    root = parse_package_root(_root(owner_id=_AUTHOR_ID, spelling="legacy"))

    emitted = json.loads(serialize_package_root(root))

    assert emitted["owners"] == [{"login": "alice", "id": _AUTHOR_ID}]
