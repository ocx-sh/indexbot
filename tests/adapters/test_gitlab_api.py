"""`adapters/gitlab_api.py` — the second `ForgePort`, under respx route mocks.

Same discipline as `tests/test_github_api.py`: one route per distinct
response class per method, assertions on the port-level return value or
exception only, never on respx internals.

The interesting tests here are the ones that have no GitHub counterpart,
because they pin the places GitLab differs in *kind* — the branch-tip
staleness check `commit_files` has to do by hand, the `create`/`update`
choice GitLab forces because it has no upsert, the source-side MR creation
for forks, and the `failure`/`error` fold onto `failed`.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
import respx

from ocx_indexbot.adapters.gitlab_api import GitLabApi
from ocx_indexbot.errors import ForgeError, TransientError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import CommitStatusState
from ocx_indexbot.ports import ForgePort

_TOKEN = "glpat-super-secret-token-value"  # noqa: S105 - test fixture, not a real credential
_API = "https://gitlab.com/api/v4"
_PROJECT = f"{_API}/projects/42"


def _client() -> GitLabApi:
    return GitLabApi(project="42", token=_TOKEN)


def _request(route: respx.Route, index: int = 0) -> httpx.Request:
    """The `index`-th request captured by `route`, typed.

    `respx.Route.calls` is untyped, and this suite asserts on request bodies
    in the places where the body *is* the contract — GitLab's `create` vs.
    `update` action, `start_sha`, `target_project_id`, the status-state fold.
    One cast here keeps every call site strict.
    """
    return cast("httpx.Request", cast("list[Any]", route.calls)[index].request)


def _body(route: respx.Route, index: int = 0) -> dict[str, Any]:
    """The JSON payload of the `index`-th request captured by `route`."""
    return cast("dict[str, Any]", json.loads(_request(route, index).content))


def test_gitlab_api_conforms_to_forge_port() -> None:
    """Structural conformance, asserted on the real adapter and not only on
    `tests/fakes`' stand-in — a method that drifted in signature would type-
    check against the fake and still fail in production."""
    port: ForgePort = _client()
    assert port is not None


# ---- get_file_contents -----------------------------------------------------


@respx.mock
def test_get_file_contents_returns_raw_bytes() -> None:
    """The `/raw` endpoint, not the JSON one — bytes arrive as bytes."""
    respx.get(f"{_PROJECT}/repository/files/p%2Fkitware%2Fcmake.json/raw").mock(
        return_value=httpx.Response(200, content=b'{"format_version":1}')
    )

    assert _client().get_file_contents("p/kitware/cmake.json", "main") == b'{"format_version":1}'


@respx.mock
def test_get_file_contents_missing_returns_none() -> None:
    respx.get(f"{_PROJECT}/repository/files/nope.json/raw").mock(
        return_value=httpx.Response(404, json={"message": "404 File Not Found"})
    )

    assert _client().get_file_contents("nope.json", "main") is None


@respx.mock
def test_a_nested_project_path_is_encoded_once() -> None:
    """A GitLab project may be a numeric id or a path with subgroups; the
    path form has to survive as a single URL segment, or every call 404s."""
    respx.get(f"{_API}/projects/acme%2Fplatform%2Findex/repository/files/config.json/raw").mock(
        return_value=httpx.Response(200, content=b"{}")
    )

    api = GitLabApi(project="acme/platform/index", token=_TOKEN)
    assert api.get_file_contents("config.json", "main") == b"{}"


# ---- get_ref_sha ------------------------------------------------------------


@respx.mock
def test_get_ref_sha_returns_branch_tip() -> None:
    respx.get(f"{_PROJECT}/repository/branches/main").mock(
        return_value=httpx.Response(200, json={"commit": {"id": "tip-sha"}})
    )

    assert _client().get_ref_sha("main") == "tip-sha"


@respx.mock
def test_get_ref_sha_missing_branch_returns_none() -> None:
    respx.get(f"{_PROJECT}/repository/branches/announce%2Fx").mock(
        return_value=httpx.Response(404, json={"message": "404 Branch Not Found"})
    )

    assert _client().get_ref_sha("announce/x") is None


# ---- transient classification (shared with the GitHub adapter) --------------


@respx.mock
def test_401_raises_transient_and_never_leaks_token() -> None:
    respx.get(f"{_PROJECT}/repository/branches/main").mock(
        return_value=httpx.Response(401, json={"message": "401 Unauthorized"})
    )

    with pytest.raises(TransientError) as exc_info:
        _client().get_ref_sha("main")

    assert _TOKEN not in str(exc_info.value)


@respx.mock
def test_429_raises_transient() -> None:
    respx.get(f"{_PROJECT}/repository/branches/main").mock(
        return_value=httpx.Response(429, json={"message": "Too many requests"})
    )

    with pytest.raises(TransientError, match="GitLab API rate limit"):
        _client().get_ref_sha("main")


@respx.mock
def test_5xx_raises_transient() -> None:
    respx.get(f"{_PROJECT}/repository/branches/main").mock(
        return_value=httpx.Response(502, json={"message": "bad gateway"})
    )

    with pytest.raises(TransientError, match="GitLab API server error: 502"):
        _client().get_ref_sha("main")


@respx.mock
def test_anonymous_client_sends_no_private_token_header() -> None:
    """`announce --out` reads a public index with no credential at all;
    sending an empty `PRIVATE-TOKEN` would itself be rejected."""
    route = respx.get(f"{_PROJECT}/repository/files/config.json/raw").mock(
        return_value=httpx.Response(200, content=b"{}")
    )

    GitLabApi(project="42").get_file_contents("config.json", "main")

    assert "PRIVATE-TOKEN" not in _request(route).headers


# ---- commit_files -----------------------------------------------------------


def _mock_branch(tip: str | None) -> None:
    response = (
        httpx.Response(404, json={"message": "404 Branch Not Found"})
        if tip is None
        else httpx.Response(200, json={"commit": {"id": tip}})
    )
    respx.get(f"{_PROJECT}/repository/branches/announce").mock(return_value=response)


@respx.mock
def test_commit_files_creates_the_branch_when_it_does_not_exist() -> None:
    _mock_branch(None)
    respx.head(f"{_PROJECT}/repository/files/p%2Fns%2Fpkg.json").mock(
        return_value=httpx.Response(404)
    )
    commit = respx.post(f"{_PROJECT}/repository/commits").mock(
        return_value=httpx.Response(201, json={"id": "new-sha"})
    )

    result = _client().commit_files(
        branch="announce", base_sha="base-sha", message="msg", files={"p/ns/pkg.json": b"{}"}
    )

    assert result == "new-sha"
    payload = _body(commit)
    assert payload["start_sha"] == "base-sha", "a new branch needs a start point"
    assert payload["actions"] == [
        {
            "action": "create",
            "file_path": "p/ns/pkg.json",
            "content": base64.b64encode(b"{}").decode("ascii"),
            "encoding": "base64",
        }
    ]


@respx.mock
def test_commit_files_updates_an_existing_path_and_omits_start_sha() -> None:
    """Two things at once, because they are the same GitLab quirk: an
    existing branch must not be given a start point, and an existing path
    must be `update` — `create` on either is a 400."""
    _mock_branch("base-sha")
    respx.head(f"{_PROJECT}/repository/files/p%2Fns%2Fpkg.json").mock(
        return_value=httpx.Response(200)
    )
    commit = respx.post(f"{_PROJECT}/repository/commits").mock(
        return_value=httpx.Response(201, json={"id": "new-sha"})
    )

    _client().commit_files(
        branch="announce", base_sha="base-sha", message="msg", files={"p/ns/pkg.json": b"{}"}
    )

    payload = _body(commit)
    assert "start_sha" not in payload
    assert payload["actions"][0]["action"] == "update"


@respx.mock
def test_commit_files_deletes_a_none_value_without_probing() -> None:
    _mock_branch("base-sha")
    commit = respx.post(f"{_PROJECT}/repository/commits").mock(
        return_value=httpx.Response(201, json={"id": "new-sha"})
    )

    _client().commit_files(
        branch="announce", base_sha="base-sha", message="msg", files={"p/ns/gone.json": None}
    )

    assert _body(commit)["actions"] == [{"action": "delete", "file_path": "p/ns/gone.json"}]


@respx.mock
def test_commit_files_refuses_a_moved_branch() -> None:
    """GitLab's commit endpoint never consults `base_sha`, so without this
    check a concurrent push would be silently absorbed — the one thing
    `ports.ForgePort.commit_files` promises never happens."""
    _mock_branch("someone-elses-sha")

    with pytest.raises(TransientError, match="moved since base_sha"):
        _client().commit_files(
            branch="announce", base_sha="base-sha", message="msg", files={"p/ns/pkg.json": b"{}"}
        )


@respx.mock
def test_commit_files_carries_gitlabs_own_refusal_message() -> None:
    """GitLab answers 400 for a lost race AND for several permanent mistakes —
    a `create` on a path that exists, an unreachable start point. Replacing
    its message with a guess is how "the branch moved" came to be printed for
    a fork that simply could not see its upstream's commit."""
    _mock_branch("base-sha")
    respx.head(f"{_PROJECT}/repository/files/p%2Fns%2Fpkg.json").mock(
        return_value=httpx.Response(404)
    )
    respx.post(f"{_PROJECT}/repository/commits").mock(
        return_value=httpx.Response(400, json={"message": "A file with this name already exists"})
    )

    with pytest.raises(TransientError, match="A file with this name already exists"):
        _client().commit_files(
            branch="announce", base_sha="base-sha", message="msg", files={"p/ns/pkg.json": b"{}"}
        )


@respx.mock
def test_a_fresh_branch_can_be_cut_from_another_project() -> None:
    """A GitLab fork shares no object storage with its upstream — measured
    2026-08-25, it answers `404 Commit Not Found` for the upstream tip and
    refuses to create a ref at it. `start_project` is the only way across, and
    without it the announce lane cannot open a fork merge request at all.

    The existence probe moves with the start point: whether a path needs
    `create` or `update` is a question about the project the commit will be
    applied on top of, not about this one.
    """
    _mock_branch(None)
    upstream_probe = respx.head(
        f"{_API}/projects/acme%2Findex/repository/files/p%2Fns%2Fpkg.json"
    ).mock(return_value=httpx.Response(200))
    commit = respx.post(f"{_PROJECT}/repository/commits").mock(
        return_value=httpx.Response(201, json={"id": "new-sha"})
    )

    _client().commit_files(
        branch="announce",
        base_sha="upstream-sha",
        message="msg",
        files={"p/ns/pkg.json": b"{}"},
        base_repo="acme/index",
    )

    payload = _body(commit)
    assert payload["start_project"] == "acme/index"
    assert payload["start_sha"] == "upstream-sha"
    assert payload["actions"][0]["action"] == "update", "the path exists at the START point"
    assert upstream_probe.called, "the probe must ask the project the commit lands on"


@respx.mock
def test_an_existing_branch_is_never_told_where_upstream_is() -> None:
    """Its own tip is already in this project, and passing a start point for a
    branch GitLab would have to fast-forward is a 400."""
    _mock_branch("base-sha")
    respx.head(f"{_PROJECT}/repository/files/p%2Fns%2Fpkg.json").mock(
        return_value=httpx.Response(200)
    )
    commit = respx.post(f"{_PROJECT}/repository/commits").mock(
        return_value=httpx.Response(201, json={"id": "new-sha"})
    )

    _client().commit_files(
        branch="announce", base_sha="base-sha", message="msg", files={"p/ns/pkg.json": b"{}"}
    )

    assert "start_project" not in _body(commit)


# ---- open_or_update_pull_request --------------------------------------------


def _mr(
    iid: int, *, source: int = 42, target: int = 42, title: str, description: str
) -> dict[str, Any]:
    return {
        "iid": iid,
        "title": title,
        "description": description,
        "source_project_id": source,
        "target_project_id": target,
    }


@respx.mock
def test_opens_a_same_project_merge_request() -> None:
    respx.get(f"{_PROJECT}/merge_requests").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{_PROJECT}/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 7})
    )

    result = _client().open_or_update_pull_request(
        branch="announce", base="main", title="t", body="b"
    )

    assert result == 7
    payload = _body(create)
    assert payload["source_branch"] == "announce"
    assert payload["target_branch"] == "main"
    assert "target_project_id" not in payload, "same-project MRs need no cross-project target"


@respx.mock
def test_opens_a_fork_merge_request_from_the_source_project() -> None:
    """The inversion: GitHub POSTs to the target with `head=owner:branch`,
    GitLab POSTs to the *fork* with `target_project_id` pointing back."""
    respx.get(f"{_API}/projects/alice%2Ffork").mock(
        return_value=httpx.Response(200, json={"id": 99})
    )
    respx.get(f"{_API}/projects/42").mock(return_value=httpx.Response(200, json={"id": 42}))
    respx.get(f"{_PROJECT}/merge_requests").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{_API}/projects/alice%2Ffork/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 12})
    )

    result = _client().open_or_update_pull_request(
        branch="announce", base="main", title="t", body="b", head_repo="alice/fork"
    )

    assert result == 12
    assert _body(create)["target_project_id"] == 42


@respx.mock
def test_reuses_the_open_merge_request_and_patches_a_changed_title() -> None:
    respx.get(f"{_PROJECT}/merge_requests").mock(
        return_value=httpx.Response(200, json=[_mr(7, title="old", description="b")])
    )
    update = respx.put(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json={"iid": 7})
    )

    assert (
        _client().open_or_update_pull_request(branch="announce", base="main", title="t", body="b")
        == 7
    )
    assert _body(update) == {"title": "t", "description": "b"}


@respx.mock
def test_an_unchanged_merge_request_is_never_patched() -> None:
    """A no-op edit would bump the MR's activity feed on every re-run."""
    respx.get(f"{_PROJECT}/merge_requests").mock(
        return_value=httpx.Response(200, json=[_mr(7, title="t", description="b")])
    )
    update = respx.put(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json={})
    )

    assert (
        _client().open_or_update_pull_request(branch="announce", base="main", title="t", body="b")
        == 7
    )
    assert not update.called


@respx.mock
def test_a_fork_merge_request_from_another_fork_is_not_reused() -> None:
    """Two forks can push the same branch name at the same target. GitLab's
    project-level MR list cannot filter by source project, so an unnarrowed
    match would have this publisher hijack someone else's MR."""
    respx.get(f"{_API}/projects/alice%2Ffork").mock(
        return_value=httpx.Response(200, json={"id": 99})
    )
    respx.get(f"{_API}/projects/42").mock(return_value=httpx.Response(200, json={"id": 42}))
    respx.get(f"{_PROJECT}/merge_requests").mock(
        return_value=httpx.Response(
            200, json=[_mr(7, source=77, target=42, title="t", description="b")]
        )
    )
    create = respx.post(f"{_API}/projects/alice%2Ffork/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 12})
    )

    result = _client().open_or_update_pull_request(
        branch="announce", base="main", title="t", body="b", head_repo="alice/fork"
    )

    assert result == 12
    assert create.called


@respx.mock
def test_a_re_announce_reuses_the_forks_own_open_merge_request() -> None:
    """The announce lane is re-run on every new tag, so the second run must
    find the MR the first one opened rather than stack a duplicate."""
    respx.get(f"{_API}/projects/alice%2Ffork").mock(
        return_value=httpx.Response(200, json={"id": 99})
    )
    respx.get(f"{_PROJECT}/merge_requests").mock(
        return_value=httpx.Response(
            200, json=[_mr(7, source=99, target=42, title="old", description="b")]
        )
    )
    update = respx.put(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json={})
    )

    result = _client().open_or_update_pull_request(
        branch="announce", base="main", title="t", body="b", head_repo="alice/fork"
    )

    assert result == 7
    assert _body(update) == {"title": "t", "description": "b"}


@respx.mock
def test_a_fork_merge_request_is_not_mistaken_for_a_same_project_one() -> None:
    respx.get(f"{_PROJECT}/merge_requests").mock(
        return_value=httpx.Response(
            200, json=[_mr(7, source=99, target=42, title="t", description="b")]
        )
    )
    create = respx.post(f"{_PROJECT}/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 8})
    )

    assert (
        _client().open_or_update_pull_request(branch="announce", base="main", title="t", body="b")
        == 8
    )
    assert create.called


# ---- labels / auto-merge -----------------------------------------------------


@respx.mock
def test_add_labels_merges_instead_of_replacing() -> None:
    update = respx.put(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json={})
    )

    _client().add_labels(7, ["indexbot:new-package", "indexbot:human-review"])

    assert _body(update) == {"add_labels": "indexbot:new-package,indexbot:human-review"}


@respx.mock
def test_remove_label_is_a_delta_not_an_assignment() -> None:
    """`remove_labels` mirrors `add_labels`: naming one label leaves every
    other label on the merge request alone, including a human's own."""
    update = respx.put(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json={})
    )

    _client().remove_label(7, "human-review-required")

    assert _body(update) == {"remove_labels": "human-review-required"}


@respx.mock
def test_enable_auto_merge_sets_merge_when_pipeline_succeeds() -> None:
    merge = respx.put(f"{_PROJECT}/merge_requests/7/merge").mock(
        return_value=httpx.Response(200, json={})
    )

    _client().enable_auto_merge(7, head_sha="c" * 40)

    assert _body(merge) == {"merge_when_pipeline_succeeds": True, "sha": "c" * 40}


@respx.mock
def test_withdraw_auto_merge_is_a_noop_when_not_armed() -> None:
    """The ordinary human-lane case. No `cancel_merge_when_pipeline_succeeds`
    route is mocked, so a call here would fail loudly if it fired anyway."""
    respx.get(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json={"merge_when_pipeline_succeeds": False})
    )

    _client().withdraw_auto_merge(7)  # no exception, no cancel call


@respx.mock
def test_withdraw_auto_merge_cancels_when_armed() -> None:
    respx.get(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json={"merge_when_pipeline_succeeds": True})
    )
    cancel = respx.post(f"{_PROJECT}/merge_requests/7/cancel_merge_when_pipeline_succeeds").mock(
        return_value=httpx.Response(200, json={})
    )

    _client().withdraw_auto_merge(7)

    assert cancel.called


# ---- get_pull_request_info ---------------------------------------------------


def _mock_mr_detail(payload: dict[str, Any]) -> None:
    respx.get(f"{_PROJECT}/merge_requests/7").mock(return_value=httpx.Response(200, json=payload))


@respx.mock
def test_get_pull_request_info_reads_diff_refs_and_author() -> None:
    _mock_mr_detail(
        {
            "diff_refs": {"base_sha": "base", "head_sha": "head", "start_sha": "start"},
            "author": {"username": "alice", "id": 4242},
            "updated_at": "2026-07-17T00:00:00Z",
            "labels": ["checks-failed", "refresh"],
        }
    )
    respx.get(f"{_PROJECT}/merge_requests/7/diffs").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"new_path": "p/ns/pkg.json", "old_path": "p/ns/pkg.json"},
                {"new_path": "p/ns/gone.json", "old_path": "p/ns/gone.json"},
            ],
        )
    )

    info = _client().get_pull_request_info(7)

    assert info.base_sha == "base"
    assert info.head_sha == "head"
    assert info.author_login == "alice"
    assert info.author_id == 4242
    assert info.changed_paths == ("p/ns/pkg.json", "p/ns/gone.json")
    assert info.updated_at == "2026-07-17T00:00:00Z"
    assert info.labels == ("checks-failed", "refresh")


@respx.mock
def test_get_pull_request_info_missing_raises_key_error() -> None:
    respx.get(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(404, json={"message": "404 Not found"})
    )

    with pytest.raises(KeyError):
        _client().get_pull_request_info(7)


def _mr_payload(head_sha: str) -> dict[str, Any]:
    """A minimal merge-request body, parameterised on the one field the
    head-movement re-read compares."""
    return {
        "diff_refs": {"base_sha": "base", "head_sha": head_sha, "start_sha": "start"},
        "author": {"username": "alice", "id": 4242},
        "updated_at": "2026-07-17T00:00:00Z",
        "labels": [],
    }


@respx.mock
def test_a_push_during_the_diff_walk_refuses_the_read() -> None:
    """`diff_refs.head_sha` comes from the merge-request entity and the paths
    from a separate paginated `/diffs` walk, with nothing binding the second
    to the first. A push landing between them returns one revision's file list
    under another's sha — which `cli/classify_pr.py` then classifies, and
    `cli/governance_poll.py` arms auto-merge against, using the `sha` guard
    that still matches.

    Same defect and same fix as `tests/test_github_api.py`'s counterpart; the
    poll lane makes it *more* reachable here, not less, since every open merge
    request is read on every tick.
    """
    respx.get(f"{_PROJECT}/merge_requests/7").mock(
        side_effect=[
            httpx.Response(200, json=_mr_payload("head")),
            httpx.Response(200, json=_mr_payload("pushed")),
        ]
    )
    respx.get(f"{_PROJECT}/merge_requests/7/diffs").mock(
        return_value=httpx.Response(200, json=[{"new_path": "p/ns/pkg.json"}])
    )

    with pytest.raises(TransientError, match="was pushed to while its diffs were read"):
        _client().get_pull_request_info(7)


@respx.mock
def test_an_unchanged_head_across_the_diff_walk_is_accepted() -> None:
    """The complementary half — the re-read must not turn every ordinary read
    into a retry."""
    _mock_mr_detail(_mr_payload("head"))
    respx.get(f"{_PROJECT}/merge_requests/7/diffs").mock(
        return_value=httpx.Response(200, json=[{"new_path": "p/ns/pkg.json"}])
    )

    assert _client().get_pull_request_info(7).head_sha == "head"


@respx.mock
def test_a_merge_request_without_diff_refs_is_transient() -> None:
    """GitLab reports `diff_refs: null` until it has computed the diff. The
    alternative to retrying is classifying a PR against invented SHAs."""
    _mock_mr_detail({"diff_refs": None, "author": {"username": "alice", "id": 1}})

    with pytest.raises(TransientError, match="no diff refs yet"):
        _client().get_pull_request_info(7)


@respx.mock
def test_a_non_object_diff_refs_is_refused() -> None:
    """`diff_refs` is `payload.get("diff_refs")`, unchecked before the fix —
    `not diff_refs` only screens out the documented `null`-until-computed
    case; a list or a scalar there is truthy and used to flow straight into
    `cast("dict[str, Any]", diff_refs)` with nothing proving the shape."""
    _mock_mr_detail(
        {"diff_refs": ["not", "an", "object"], "author": {"username": "alice", "id": 1}}
    )

    with pytest.raises(ForgeError, match="non-object diff_refs"):
        _client().get_pull_request_info(7)


@respx.mock
def test_get_pull_request_info_refuses_a_non_object_merge_request_body() -> None:
    """`_merge_request` used to `cast` the decoded body with nothing proving
    it was ever a JSON object — mirrors `github_api.py`'s identical guard."""
    respx.get(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json=["unexpected"])
    )

    with pytest.raises(ForgeError, match="non-object body"):
        _client().get_pull_request_info(7)


# ---- find_pull_request_by_head_sha (WP5-C, ADR-6 FP-8) ------------------------


@respx.mock
def test_find_pull_request_by_head_sha_exact_match_same_project_is_not_a_fork() -> None:
    respx.get(f"{_PROJECT}/repository/commits/deadbeef/merge_requests").mock(
        return_value=httpx.Response(
            200,
            json=[{"iid": 9, "sha": "deadbeef", "source_project_id": 42, "target_project_id": 42}],
        )
    )

    result = _client().find_pull_request_by_head_sha("deadbeef")

    assert result is not None
    assert (result.number, result.is_fork) == (9, False)


@respx.mock
def test_find_pull_request_by_head_sha_exact_match_fork_is_a_fork() -> None:
    respx.get(f"{_PROJECT}/repository/commits/deadbeef/merge_requests").mock(
        return_value=httpx.Response(
            200,
            json=[{"iid": 9, "sha": "deadbeef", "source_project_id": 99, "target_project_id": 42}],
        )
    )

    result = _client().find_pull_request_by_head_sha("deadbeef")

    assert result is not None
    assert (result.number, result.is_fork) == (9, True)


@respx.mock
def test_find_pull_request_by_head_sha_ignores_a_pr_whose_head_moved_on() -> None:
    """The head-sha filter, not "first result": this endpoint answers "which
    MRs is this commit associated with", and one still-open MR listed here may
    have moved past `deadbeef` since (a rebase or a new push)."""
    respx.get(f"{_PROJECT}/repository/commits/deadbeef/merge_requests").mock(
        return_value=httpx.Response(
            200,
            json=[{"iid": 9, "sha": "newer-sha", "source_project_id": 99, "target_project_id": 42}],
        )
    )

    assert _client().find_pull_request_by_head_sha("deadbeef") is None


@respx.mock
def test_find_pull_request_by_head_sha_no_association_returns_none() -> None:
    respx.get(f"{_PROJECT}/repository/commits/deadbeef/merge_requests").mock(
        return_value=httpx.Response(200, json=[])
    )

    assert _client().find_pull_request_by_head_sha("deadbeef") is None


@respx.mock
def test_find_pull_request_by_head_sha_unknown_commit_returns_none() -> None:
    respx.get(f"{_PROJECT}/repository/commits/deadbeef/merge_requests").mock(
        return_value=httpx.Response(404, json={"message": "404 Commit Not Found"})
    )

    assert _client().find_pull_request_by_head_sha("deadbeef") is None


@respx.mock
def test_find_pull_request_by_head_sha_refuses_a_non_list_body() -> None:
    """Single, unpaginated `GET` — same guard `github_api.py`'s counterpart
    needs, same failure mode (`item.get(...)` on a dict's keys) without it."""
    respx.get(f"{_PROJECT}/repository/commits/deadbeef/merge_requests").mock(
        return_value=httpx.Response(200, json={"message": "unexpected"})
    )

    with pytest.raises(ForgeError, match="non-list body"):
        _client().find_pull_request_by_head_sha("deadbeef")


# ---- close_pull_request (WP5-C) ------------------------------------------------


@respx.mock
def test_close_pull_request_sets_state_event_close() -> None:
    update = respx.put(f"{_PROJECT}/merge_requests/9").mock(
        return_value=httpx.Response(200, json={})
    )

    _client().close_pull_request(9)

    assert _body(update) == {"state_event": "close"}


# ---- set_commit_status -------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [("success", "success"), ("pending", "pending"), ("failure", "failed"), ("error", "failed")],
)
@respx.mock
def test_commit_status_states_map_onto_gitlabs_vocabulary(
    state: CommitStatusState, expected: str
) -> None:
    """GitLab has no `error`; both failing states must land on `failed` or
    the governance gate silently posts nothing at all."""
    post = respx.post(f"{_PROJECT}/statuses/head-sha").mock(
        return_value=httpx.Response(201, json={})
    )

    _client().set_commit_status(
        "head-sha", context="governance/review-required", state=state, description="why"
    )

    assert _body(post) == {
        "state": expected,
        "name": "governance/review-required",
        "description": "why",
    }


@respx.mock
def test_a_fork_merge_requests_status_needs_the_merge_request_ref() -> None:
    """A fork merge request's head commit reaches the parent only through
    `refs/merge-requests/<iid>/head`, and GitLab's status API 404s on a commit
    it cannot place on a ref. Measured against gitlab.com on 2026-08-25: the
    same POST without `ref` fails, with it succeeds."""
    post = respx.post(f"{_PROJECT}/statuses/head-sha").mock(
        return_value=httpx.Response(201, json={})
    )

    _client().set_commit_status(
        "head-sha",
        context="governance/review-required",
        state="pending",
        description="why",
        pull_request=42,
    )

    assert _body(post)["ref"] == "refs/merge-requests/42/head"


# ---- request_reviewers -------------------------------------------------------


@respx.mock
def test_request_reviewers_resolves_usernames_to_ids() -> None:
    respx.get(f"{_API}/users", params={"username": "bob"}).mock(
        return_value=httpx.Response(200, json=[{"id": 11}])
    )
    respx.get(f"{_API}/users", params={"username": "carol"}).mock(
        return_value=httpx.Response(200, json=[{"id": 22}])
    )
    update = respx.put(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json={})
    )

    _client().request_reviewers(7, ["bob", "carol"])

    assert _body(update) == {"reviewer_ids": [11, 22]}


@respx.mock
def test_request_reviewers_with_no_logins_is_a_no_op() -> None:
    """`reviewer_ids` replaces the set, so an empty call would clear whatever
    reviewers a human had assigned."""
    update = respx.put(f"{_PROJECT}/merge_requests/7").mock(
        return_value=httpx.Response(200, json={})
    )

    _client().request_reviewers(7, [])

    assert not update.called


@respx.mock
def test_an_unknown_reviewer_username_raises() -> None:
    """A maintainer who is not a user is a bug in `.github/maintainers.yml`.
    GitHub answers 422 and lets it propagate; dropping it silently here
    would leave a human-lane MR with no reviewer and no complaint."""
    respx.get(f"{_API}/users", params={"username": "ghost"}).mock(
        return_value=httpx.Response(200, json=[])
    )

    with pytest.raises(LookupError, match="ghost"):
        _client().request_reviewers(7, ["ghost"])


@respx.mock
def test_re_posting_the_state_a_commit_already_holds_is_a_no_op() -> None:
    """A GitLab commit status is a state machine: re-posting the state it is
    already in is an illegal transition, not a no-op. `pending` is the human
    lane's steady state, so the second poll tick of any merge request awaiting
    review hits this — and did, in production, ending the whole sweep."""
    respx.post(f"{_PROJECT}/statuses/{'a' * 40}").mock(
        return_value=httpx.Response(400, json={"message": "Cannot transition status"})
    )
    respx.get(f"{_PROJECT}/repository/commits/{'a' * 40}/statuses").mock(
        return_value=httpx.Response(
            200, json=[{"name": "governance/review-required", "status": "pending"}]
        )
    )

    _client().set_commit_status(
        "a" * 40,
        context="governance/review-required",
        state="pending",
        description="awaiting review",
    )


@respx.mock
def test_the_existing_status_is_found_past_the_first_page() -> None:
    """`/repository/commits/<sha>/statuses` lists *every* status on the commit
    and GitLab's default page holds 20 — a commit carrying a handful of
    pipeline jobs pushes `governance/review-required` off page 1 easily.

    An unpaginated read fails closed: it does not find the status, so the 400
    re-raises. Safe, and also exactly the production failure this method
    exists to stop — the sweep ending on a merge request sitting in its steady
    `pending` state. No raise below is the assertion.
    """
    respx.post(f"{_PROJECT}/statuses/{'a' * 40}").mock(
        return_value=httpx.Response(400, json={"message": "Cannot transition status"})
    )
    page_two = f"{_PROJECT}/repository/commits/{'a' * 40}/statuses?page=2"
    respx.get(
        f"{_PROJECT}/repository/commits/{'a' * 40}/statuses", params={"per_page": "100"}
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"name": "build", "status": "success"}],
            headers={"Link": f'<{page_two}>; rel="next"'},
        )
    )
    respx.get(page_two).mock(
        return_value=httpx.Response(
            200, json=[{"name": "governance/review-required", "status": "pending"}]
        )
    )

    _client().set_commit_status(
        "a" * 40,
        context="governance/review-required",
        state="pending",
        description="awaiting review",
    )


@respx.mock
def test_a_400_that_is_not_the_state_machine_still_raises() -> None:
    """Only 'already in that state' is benign. Anything else the endpoint
    refuses is a real failure and must not be swallowed by the widened path."""
    respx.post(f"{_PROJECT}/statuses/{'a' * 40}").mock(
        return_value=httpx.Response(400, json={"message": "invalid ref"})
    )
    respx.get(f"{_PROJECT}/repository/commits/{'a' * 40}/statuses").mock(
        return_value=httpx.Response(200, json=[])
    )

    with pytest.raises(ForgeError, match="400"):
        _client().set_commit_status(
            "a" * 40,
            context="governance/review-required",
            state="pending",
            description="awaiting review",
        )


# ---- create_comment: the merge gate, not a comment ---------------------------

_MARKER = "<!-- indexbot:governance -->"
_DISCUSSIONS = f"{_PROJECT}/merge_requests/7/discussions"


_SELF_ID = 1001


def _self_user() -> respx.Route:
    """`GET /user` — how the adapter learns which notes are its own."""
    return respx.get(f"{_API}/user").mock(
        return_value=httpx.Response(200, json={"id": _SELF_ID, "username": "indexbot"})
    )


def _discussion(
    did: str,
    note_id: int,
    body: str,
    *,
    resolvable: bool = True,
    author_id: int = _SELF_ID,
) -> dict[str, Any]:
    return {
        "id": did,
        "notes": [
            {"id": note_id, "body": body, "resolvable": resolvable, "author": {"id": author_id}}
        ],
    }


@respx.mock
def test_create_comment_opens_a_resolvable_discussion_not_a_note() -> None:
    """A plain note blocks nothing. An unresolved *discussion* makes
    `detailed_merge_status` report `discussions_not_resolved` — which is what
    holds a fork merge request, where an external commit status does not."""
    _self_user()
    respx.get(_DISCUSSIONS).mock(
        return_value=httpx.Response(200, json=[_discussion("d1", 1, "unrelated chatter")])
    )
    post = respx.post(_DISCUSSIONS).mock(return_value=httpx.Response(201, json={"id": "d2"}))

    _client().create_comment(7, f"{_MARKER}\nreview required", marker=_MARKER)

    assert _body(post)["body"].endswith("review required")


@respx.mock
def test_create_comment_updates_the_marked_thread_in_place() -> None:
    _self_user()
    respx.get(_DISCUSSIONS).mock(
        return_value=httpx.Response(200, json=[_discussion("d5", 55, f"{_MARKER}\nold")])
    )
    update = respx.put(f"{_DISCUSSIONS}/d5/notes/55").mock(
        return_value=httpx.Response(200, json={})
    )
    reopen = respx.put(f"{_DISCUSSIONS}/d5").mock(return_value=httpx.Response(200, json={}))

    _client().create_comment(7, f"{_MARKER}\nnew", marker=_MARKER)

    assert _body(update) == {"body": f"{_MARKER}\nnew"}
    assert _body(reopen) == {"resolved": False}


@respx.mock
def test_an_identical_body_still_re_opens_the_thread() -> None:
    """The body is idempotent; the *resolution* is not. A maintainer who
    resolved the thread and then pushed a change that still needs review must
    not find the gate already released."""
    body = f"{_MARKER}\nsame"
    _self_user()
    respx.get(_DISCUSSIONS).mock(
        return_value=httpx.Response(200, json=[_discussion("d5", 55, body)])
    )
    update = respx.put(f"{_DISCUSSIONS}/d5/notes/55").mock(
        return_value=httpx.Response(200, json={})
    )
    reopen = respx.put(f"{_DISCUSSIONS}/d5").mock(return_value=httpx.Response(200, json={}))

    _client().create_comment(7, body, marker=_MARKER)

    assert not update.called, "the note body did not change"
    assert _body(reopen) == {"resolved": False}


@respx.mock
def test_a_thread_with_no_notes_is_skipped() -> None:
    """GitLab returns system-event discussions with an empty `notes` array;
    indexing into one would crash the whole governance sweep."""
    _self_user()
    respx.get(_DISCUSSIONS).mock(
        return_value=httpx.Response(200, json=[{"id": "sys", "notes": []}])
    )
    post = respx.post(_DISCUSSIONS).mock(return_value=httpx.Response(201, json={"id": "d2"}))

    _client().create_comment(7, f"{_MARKER}\nreview required", marker=_MARKER)

    assert post.called


@respx.mock
@pytest.mark.parametrize("status", [406, 409])
def test_a_moved_head_does_not_fail_the_auto_merge_arm(status: int) -> None:
    """`sha` is GitLab's optimistic-concurrency guard: the merge is refused
    unless it still names the source branch head. A refusal means the author
    pushed between the classification and this call, so the decision was about
    a revision that is no longer current — the next poll tick gates the new
    one. Raising here would take the whole sweep down over a race."""
    respx.put(f"{_PROJECT}/merge_requests/7/merge").mock(
        return_value=httpx.Response(status, json={"message": "SHA does not match HEAD of source"})
    )

    _client().enable_auto_merge(7, head_sha="c" * 40)  # no exception = handled


@respx.mock
def test_a_refused_auto_merge_arm_is_reported_not_silently_swallowed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """406 is GitLab's *generic* not-mergeable answer: a moved head, yes, but
    also a conflict, a draft, or unresolved discussions — none of which clear
    on the next tick. Swallowing it is right (none is this call's failure);
    swallowing it silently is not, because `cli/governance_poll.py` then
    prints `refresh -> success` for a merge request nothing armed, which is
    the opposite of what that per-merge-request line exists to tell a human.

    GitLab's own message is the only thing that separates the four causes, so
    it is carried through to stderr where the sweep's own lines go.
    """
    respx.put(f"{_PROJECT}/merge_requests/7/merge").mock(
        return_value=httpx.Response(406, json={"message": "Branch cannot be merged"})
    )

    _client().enable_auto_merge(7, head_sha="c" * 40)

    reported = capsys.readouterr().err
    assert "!7" in reported
    assert "Branch cannot be merged" in reported


@respx.mock
def test_a_refused_auto_merge_arm_still_raises_for_any_other_reason() -> None:
    respx.put(f"{_PROJECT}/merge_requests/7/merge").mock(
        return_value=httpx.Response(403, json={"message": "insufficient permissions"})
    )

    with pytest.raises(ForgeError, match="403"):
        _client().enable_auto_merge(7, head_sha="c" * 40)


@respx.mock
def test_a_plain_note_carrying_the_marker_never_counts_as_the_gate() -> None:
    """The marker ships in every notice this bot posts, so a merge request's
    author can copy it into a note of their own. A note is not resolvable, so
    it reaches no `detailed_merge_status` — matching one would make the bot
    believe the thread exists and never open the one that actually blocks the
    merge. That is the human-review gate disarmed by a comment."""
    _self_user()
    respx.get(_DISCUSSIONS).mock(
        return_value=httpx.Response(
            200,
            json=[_discussion("d9", 91, f"{_MARKER}\nnothing to see here", resolvable=False)],
        )
    )
    post = respx.post(_DISCUSSIONS).mock(return_value=httpx.Response(201, json={"id": "d2"}))

    _client().create_comment(7, f"{_MARKER}\nreview required", marker=_MARKER)

    assert post.called, "a real, resolvable thread must still be opened"


@respx.mock
def test_a_resolvable_thread_opened_by_someone_else_never_counts_as_the_gate() -> None:
    """Same bypass by the other route: a thread its own author can resolve is
    not a gate. Only a thread this token opened counts."""
    _self_user()
    respx.get(_DISCUSSIONS).mock(
        return_value=httpx.Response(
            200, json=[_discussion("d9", 91, f"{_MARKER}\nplease merge", author_id=4242)]
        )
    )
    post = respx.post(_DISCUSSIONS).mock(return_value=httpx.Response(201, json={"id": "d2"}))

    _client().create_comment(7, f"{_MARKER}\nreview required", marker=_MARKER)

    assert post.called


@respx.mock
def test_the_token_identity_is_resolved_once_per_instance() -> None:
    """`GET /user` cannot change inside a run, and a poll sweep asks the
    marker question once per open merge request."""
    user = _self_user()
    respx.get(_DISCUSSIONS).mock(
        return_value=httpx.Response(
            200, json=[_discussion("d9", 91, f"{_MARKER}\nplease merge", author_id=4242)]
        )
    )
    respx.post(_DISCUSSIONS).mock(return_value=httpx.Response(201, json={"id": "d2"}))

    client = _client()
    client.create_comment(7, f"{_MARKER}\na", marker=_MARKER)
    client.create_comment(7, f"{_MARKER}\nb", marker=_MARKER)

    assert user.call_count == 1


def test_a_reproject_does_not_inherit_the_original_instances_cache() -> None:
    """`commit_files` builds a differently scoped adapter with
    `dataclasses.replace(self, project=...)`, and `replace` copies every
    `init=True` field **by reference** — so an `init=True` `_cache` would hand
    the new instance the original's dict, project-scoped entries included.

    Nothing is wrongly scoped today: the only key is the token's own user id,
    which does not depend on the project. That is exactly why this needs a
    test rather than a comment — the next per-project answer memoised here
    would be answered for the wrong project, silently, with no diff at the
    call site. `init=False` makes `replace` start the copy empty.
    """
    original = _client()
    original._cache["self_user_id"] = _SELF_ID  # pyright: ignore[reportPrivateUsage]

    reprojected = replace(original, project="99")

    assert reprojected._cache == {}  # pyright: ignore[reportPrivateUsage]
    assert original._cache == {"self_user_id": _SELF_ID}  # pyright: ignore[reportPrivateUsage]


@respx.mock
def test_an_unauthenticated_adapter_asks_no_identity_question() -> None:
    """`announce --out` runs tokenless against public reads; `GET /user` would
    401 there, and there is nothing to identify."""
    user = _self_user()
    respx.get(_DISCUSSIONS).mock(
        return_value=httpx.Response(200, json=[_discussion("d9", 91, f"{_MARKER}\nhi")])
    )

    tokenless = GitLabApi(project="42")
    assert tokenless._find_marked_discussion(7, _MARKER) is None  # pyright: ignore[reportPrivateUsage]
    assert not user.called


# ---- resolve_review_thread: releasing the gate -------------------------------


@respx.mock
def test_resolve_review_thread_resolves_the_marked_discussion() -> None:
    _self_user()
    respx.get(_DISCUSSIONS).mock(
        return_value=httpx.Response(200, json=[_discussion("d5", 55, f"{_MARKER}\nblocked")])
    )
    resolve = respx.put(f"{_DISCUSSIONS}/d5").mock(return_value=httpx.Response(200, json={}))

    _client().resolve_review_thread(7, marker=_MARKER)

    assert _body(resolve) == {"resolved": True}


@respx.mock
def test_resolving_when_no_thread_exists_creates_nothing() -> None:
    """A merge request that was green from the start must not acquire a
    comment saying review was not required."""
    _self_user()
    respx.get(_DISCUSSIONS).mock(return_value=httpx.Response(200, json=[]))
    post = respx.post(_DISCUSSIONS).mock(return_value=httpx.Response(201, json={}))

    _client().resolve_review_thread(7, marker=_MARKER)

    assert not post.called


# ---- create_or_update_issue --------------------------------------------------


@respx.mock
def test_create_issue_when_no_open_issue_matches_the_title() -> None:
    respx.get(f"{_PROJECT}/issues").mock(
        return_value=httpx.Response(200, json=[{"iid": 3, "title": "something else"}])
    )
    post = respx.post(f"{_PROJECT}/issues").mock(return_value=httpx.Response(201, json={"iid": 9}))

    result = _client().create_or_update_issue(
        title="Anomaly: ns/pkg", body="details", labels=["indexbot:anomaly"]
    )

    assert result == 9
    assert _body(post) == {
        "title": "Anomaly: ns/pkg",
        "description": "details",
        "labels": "indexbot:anomaly",
    }


@respx.mock
def test_update_the_matching_open_issue_body() -> None:
    respx.get(f"{_PROJECT}/issues").mock(
        return_value=httpx.Response(
            200, json=[{"iid": 3, "title": "Anomaly: ns/pkg", "description": "stale"}]
        )
    )
    update = respx.put(f"{_PROJECT}/issues/3").mock(return_value=httpx.Response(200, json={}))

    assert _client().create_or_update_issue(title="Anomaly: ns/pkg", body="fresh") == 3
    assert _body(update) == {"description": "fresh"}


@respx.mock
def test_an_unchanged_issue_body_is_never_patched() -> None:
    respx.get(f"{_PROJECT}/issues").mock(
        return_value=httpx.Response(
            200, json=[{"iid": 3, "title": "Anomaly: ns/pkg", "description": "same"}]
        )
    )
    update = respx.put(f"{_PROJECT}/issues/3").mock(return_value=httpx.Response(200, json={}))

    assert _client().create_or_update_issue(title="Anomaly: ns/pkg", body="same") == 3
    assert not update.called


# ---- list_approvals ----------------------------------------------------------

_HEAD = "f" * 40
_PUSHED_AT = "2026-08-25T10:00:00.000Z"


def _versions(*, head: str = _HEAD, created_at: str = _PUSHED_AT) -> respx.Route:
    """The merge request's diff versions — a push produces a new one, so the
    newest version's `created_at` is when the source branch last moved."""
    return respx.get(f"{_PROJECT}/merge_requests/7/versions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 2, "created_at": created_at, "head_commit_sha": head},
                {"id": 1, "created_at": "2026-08-24T09:00:00.000Z", "head_commit_sha": "a" * 40},
            ],
        )
    )


_BOB, _CAROL = 11, 22
"""Approver user ids. `list_approvals` joins its two reads on `user.id` and
reports ids, so the usernames beside them are payload decoration — see
`test_an_approval_is_joined_and_reported_by_user_id`."""


def _approved_by(*users: tuple[str, int]) -> respx.Route:
    return respx.get(f"{_PROJECT}/merge_requests/7/approvals").mock(
        return_value=httpx.Response(
            200,
            json={
                "approved_by": [{"user": {"username": login, "id": uid}} for login, uid in users]
            },
        )
    )


def _approval_events(*pairs: tuple[tuple[str, int], str], iid: int = 7) -> respx.Route:
    return respx.get(f"{_API}/projects/42/events").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "action_name": "approved",
                    "target_type": "MergeRequest",
                    "target_iid": iid,
                    "created_at": created_at,
                    "author": {"username": login, "id": uid},
                }
                for (login, uid), created_at in pairs
            ],
        )
    )


@respx.mock
def test_an_approval_granted_after_the_last_push_counts() -> None:
    """Approving a merge request is Free; only *requiring* approvals is not.
    That asymmetry is the whole design: the gate is a commit status this bot
    posts, and an approval is what releases it."""
    _approved_by(("carol", _CAROL), ("bob", _BOB))
    _versions()
    _approval_events(
        (("bob", _BOB), "2026-08-25T11:00:00.000Z"),
        (("carol", _CAROL), "2026-08-25T12:00:00.000Z"),
    )

    assert _client().list_approvals(7, head_sha=_HEAD) == (_BOB, _CAROL)


@respx.mock
def test_an_approval_is_joined_and_reported_by_user_id() -> None:
    """`list_approvals` intersects two endpoints, and the join key is the
    authorization decision.

    A GitLab username is renameable and, once released, claimable by someone
    else — so joining `/approvals` to the events feed on `username`, or
    reporting one to `cli/governance_check.py`, would hand a former
    maintainer's veto over the human lane to whoever holds their old name. An
    approval outranks every disposition the gate can reach, including
    `governance.auto_merge = never`, so this is the strongest authorization
    signal in the bot.

    The payload below gives each user a *different* username on each of the
    two endpoints, which is exactly what a rename between the approval event
    and this read looks like. Joining on the name yields nothing; joining on
    the id yields the approver. A revert to usernames turns this green tuple
    empty.
    """
    _approved_by(("carol-new-name", _CAROL))
    _versions()
    _approval_events(((("carol-old-name"), _CAROL), "2026-08-25T11:00:00.000Z"))

    assert _client().list_approvals(7, head_sha=_HEAD) == (_CAROL,)


@respx.mock
def test_an_approval_granted_before_the_last_push_does_not_count() -> None:
    """Approval replay, and the reason this cannot be left to the project
    setting: "Remove all approvals when commits are added" is Premium — on
    Free the write is accepted and the value stays `false` (measured on
    gitlab.com). Approve revision A, push unreviewed revision B, and the
    auto-merge lane would act on A's approval."""
    _approved_by(("bob", _BOB))
    _versions()
    _approval_events((("bob", _BOB), "2026-08-25T09:59:00.000Z"))

    assert _client().list_approvals(7, head_sha=_HEAD) == ()


@respx.mock
def test_an_approval_on_another_merge_request_is_not_borrowed() -> None:
    """The events endpoint is project-wide, so the merge request has to be
    selected out of it rather than assumed."""
    _approved_by(("bob", _BOB))
    _versions()
    _approval_events((("bob", _BOB), "2026-08-25T11:00:00.000Z"), iid=8)

    assert _client().list_approvals(7, head_sha=_HEAD) == ()


@respx.mock
def test_a_head_that_is_no_longer_newest_yields_no_approvals() -> None:
    """The branch moved between the classification and this read. Fail closed:
    the next sweep gates the new revision, and nothing is lost."""
    _approved_by(("bob", _BOB))
    _versions(head="b" * 40)

    assert _client().list_approvals(7, head_sha=_HEAD) == ()


@respx.mock
def test_a_merge_request_with_no_versions_yields_no_approvals() -> None:
    """Nothing to compare an approval against — fail closed rather than treat
    an unknown push time as "never pushed"."""
    _approved_by(("bob", _BOB))
    respx.get(f"{_PROJECT}/merge_requests/7/versions").mock(
        return_value=httpx.Response(200, json=[])
    )

    assert _client().list_approvals(7, head_sha=_HEAD) == ()


@respx.mock
def test_no_approvals_is_an_empty_tuple_not_a_missing_key() -> None:
    """GitLab omits `approved_by` entirely on an unapproved MR; reading it as
    absent-means-none is what keeps the human lane from crashing on the
    common case — and it must not cost the two extra reads."""
    approvals = respx.get(f"{_PROJECT}/merge_requests/7/approvals").mock(
        return_value=httpx.Response(200, json={"id": 7})
    )
    versions = respx.get(f"{_PROJECT}/merge_requests/7/versions").mock(
        return_value=httpx.Response(200, json=[])
    )

    assert _client().list_approvals(7, head_sha=_HEAD) == ()

    assert approvals.called
    assert not versions.called


@respx.mock
def test_the_events_query_is_bounded_by_the_day_before_the_push() -> None:
    """A project-wide events feed would otherwise page back through every
    approval the project ever recorded. The bound is a date and one day wide,
    because GitLab's `after` is exclusive and project-timezone-local;
    freshness itself is decided by the timestamp comparison, not by this."""
    _approved_by(("bob", _BOB))
    _versions()
    events = _approval_events((("bob", _BOB), "2026-08-25T11:00:00.000Z"))

    _client().list_approvals(7, head_sha=_HEAD)

    assert _request(events).url.params["after"] == "2026-08-24"


# ---- list_open_pull_requests -------------------------------------------------


@respx.mock
def test_list_open_merge_requests_returns_ascending_iids() -> None:
    """The poll lane's entry point. Ascending because the sweep's per-MR
    stderr lines are read in order by a human, and an API-ordered sweep would
    shuffle them run to run."""
    respx.get(f"{_PROJECT}/merge_requests", params={"state": "opened"}).mock(
        return_value=httpx.Response(200, json=[{"iid": 12}, {"iid": 3}, {"iid": 7}])
    )

    assert _client().list_open_pull_requests() == (3, 7, 12)


# ---- pagination --------------------------------------------------------------


@respx.mock
def test_pagination_follows_the_link_header() -> None:
    """GitLab emits RFC 5988 `Link` headers exactly as GitHub does, which is
    why one shared walker serves both adapters."""
    page_two = f"{_PROJECT}/issues?page=2"
    respx.get(f"{_PROJECT}/issues", params={"state": "opened", "per_page": "100"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"iid": 1, "title": "other"}],
            headers={"Link": f'<{page_two}>; rel="next"'},
        )
    )
    respx.get(page_two).mock(
        return_value=httpx.Response(
            200, json=[{"iid": 2, "title": "Anomaly: ns/pkg", "description": "same"}]
        )
    )

    assert _client().create_or_update_issue(title="Anomaly: ns/pkg", body="same") == 2


@respx.mock
def test_the_refusal_itself_names_the_state_and_that_is_enough() -> None:
    """GitLab writes the current state into the refusal it just sent, about
    the same (project, sha, ref) the POST addressed. That answer cannot be
    scoped differently and cannot race a second call, so it is read first and
    the status listing is never fetched.

    The listing is deliberately mocked to DISAGREE here: a `sideeffect` that
    fails the test if called would prove only that it was skipped, whereas an
    empty listing proves the message alone carried the decision. Watched red
    against the listing-first implementation, which re-raised.
    """
    respx.post(f"{_PROJECT}/statuses/{'a' * 40}").mock(
        return_value=httpx.Response(
            400,
            json={
                "message": (
                    "Cannot transition status via :enqueue from :pending "
                    '(Reason(s): Status cannot transition via "enqueue")'
                )
            },
        )
    )
    listing = respx.get(f"{_PROJECT}/repository/commits/{'a' * 40}/statuses").mock(
        return_value=httpx.Response(200, json=[])
    )

    _client().set_commit_status(
        "a" * 40,
        context="governance/review-required",
        state="pending",
        description="awaiting review",
    )

    assert not listing.called


@respx.mock
def test_a_refusal_naming_a_different_state_still_raises() -> None:
    """The message is a decision, not a blanket amnesty for 400s. A context
    holding `running` when the gate wants `pending` is a real disagreement
    about whether this merge request is blocked, and swallowing it would leave
    the gate reporting something nobody chose."""
    respx.post(f"{_PROJECT}/statuses/{'a' * 40}").mock(
        return_value=httpx.Response(
            400, json={"message": "Cannot transition status via :enqueue from :running"}
        )
    )
    respx.get(f"{_PROJECT}/repository/commits/{'a' * 40}/statuses").mock(
        return_value=httpx.Response(200, json=[])
    )

    with pytest.raises(ForgeError, match="Cannot transition"):
        _client().set_commit_status(
            "a" * 40,
            context="governance/review-required",
            state="pending",
            description="awaiting review",
        )


@respx.mock
def test_a_read_timeout_is_transient_not_an_unhandled_exception() -> None:
    """Exit 75, not a traceback.

    A timeout is an exception rather than a status, so `check_transient` never
    sees it. Before this it escaped every handler in the adapter *and*
    `cli/main.py`'s `IndexBotError` branch, ending the run on a bare traceback
    with exit 1 — the code that means "this merge request is invalid" — for a
    network blip. Measured in production, not inferred:
    `governance-poll: #10: ReadTimeout: The read operation timed out`, exit 1,
    against a merge request that was perfectly valid.
    """
    respx.get(f"{_PROJECT}/repository/branches/main").mock(
        side_effect=httpx.ReadTimeout("The read operation timed out")
    )

    with pytest.raises(TransientError) as caught:
        _client().get_ref_sha("main")

    assert caught.value.exit_code is ExitCode.TRANSIENT
    assert "ReadTimeout" in str(caught.value), "which failure it was, for the operator"
