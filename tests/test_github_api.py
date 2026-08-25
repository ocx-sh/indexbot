"""`adapters/github_api.py` — respx route mocks (CONTRACTS.md §2, §10).

One `respx.mock` route per distinct response class per method. Assertions
target the port-level return value/exception only, never respx call
internals, so this suite survives an adapter refactor (CONTRACTS.md §2).
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

import httpx
import pytest
import respx

from ocx_indexbot.adapters.github_api import GitHubApi, GraphQLError
from ocx_indexbot.errors import ForgeError, TransientError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import PullRequestHeadMatch, PullRequestInfo

_TOKEN = "ghp_super-secret-token-value"  # noqa: S105 - test fixture, not a real credential


def _client() -> GitHubApi:
    return GitHubApi(owner="ocx-sh", repo="index", token=_TOKEN)


# ---- get_file_contents -----------------------------------------------------


@respx.mock
def test_get_file_contents_success() -> None:
    encoded = base64.b64encode(b'{"format_version":1}').decode("ascii")
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/contents/p/kitware/cmake.json",
        params={"ref": "main"},
    ).mock(return_value=httpx.Response(200, json={"content": encoded, "encoding": "base64"}))

    result = _client().get_file_contents("p/kitware/cmake.json", "main")

    assert result == b'{"format_version":1}'


@respx.mock
def test_get_file_contents_missing_returns_none() -> None:
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/contents/p/nobody/nothing.json",
        params={"ref": "main"},
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

    assert _client().get_file_contents("p/nobody/nothing.json", "main") is None


@respx.mock
def test_get_file_contents_anonymous_client_omits_authorization_header() -> None:
    # `cli/announce.py`'s `--out` mode reads the index repo anonymously —
    # an empty `token` must never produce a malformed `Authorization: Bearer `
    # header.
    encoded = base64.b64encode(b'{"format_version":1}').decode("ascii")
    route = respx.get(
        "https://api.github.com/repos/ocx-sh/index/contents/config.json",
        params={"ref": "main"},
    ).mock(return_value=httpx.Response(200, json={"content": encoded, "encoding": "base64"}))

    anonymous = GitHubApi(owner="ocx-sh", repo="index")
    anonymous.get_file_contents("config.json", "main")

    assert "Authorization" not in route.calls.last.request.headers


# ---- get_ref_sha ------------------------------------------------------------


@respx.mock
def test_get_ref_sha_success() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "abc123"}})
    )

    assert _client().get_ref_sha("main") == "abc123"


@respx.mock
def test_get_ref_sha_missing_branch_returns_none() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/does-not-exist").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    assert _client().get_ref_sha("does-not-exist") is None


# ---- shared transient-status behavior (401 / 403+Retry-After / 429 / 5xx) --


@respx.mock
def test_401_raises_transient_and_never_leaks_token() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/main").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )

    with pytest.raises(TransientError) as exc_info:
        _client().get_ref_sha("main")

    assert _TOKEN not in str(exc_info.value)


@respx.mock
def test_403_rate_limit_with_retry_after_raises_transient() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/main").mock(
        return_value=httpx.Response(
            403, headers={"Retry-After": "30"}, json={"message": "rate limited"}
        )
    )

    with pytest.raises(TransientError, match="retry after 30s"):
        _client().get_ref_sha("main")


@respx.mock
def test_403_without_retry_after_is_not_treated_as_rate_limit() -> None:
    # Plain permission-denied 403 (no Retry-After) is a config bug, not a
    # retryable condition — left to raise_for_status() as an unhandled error.
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/main").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )

    with pytest.raises(ForgeError, match="403"):
        _client().get_ref_sha("main")


@respx.mock
def test_429_raises_transient() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/main").mock(
        return_value=httpx.Response(429, json={"message": "too many requests"})
    )

    with pytest.raises(TransientError, match="rate limit"):
        _client().get_ref_sha("main")


@respx.mock
def test_5xx_raises_transient() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/main").mock(
        return_value=httpx.Response(503, json={"message": "service unavailable"})
    )

    with pytest.raises(TransientError, match="server error: 503"):
        _client().get_ref_sha("main")


# ---- commit_files -----------------------------------------------------------


def _mock_blob_tree_commit(base_sha: str = "base-sha") -> None:
    respx.get(f"https://api.github.com/repos/ocx-sh/index/git/commits/{base_sha}").mock(
        return_value=httpx.Response(200, json={"tree": {"sha": "base-tree-sha"}})
    )
    respx.post("https://api.github.com/repos/ocx-sh/index/git/blobs").mock(
        return_value=httpx.Response(201, json={"sha": "blob-sha"})
    )
    respx.post("https://api.github.com/repos/ocx-sh/index/git/trees").mock(
        return_value=httpx.Response(201, json={"sha": "new-tree-sha"})
    )
    respx.post("https://api.github.com/repos/ocx-sh/index/git/commits").mock(
        return_value=httpx.Response(201, json={"sha": "new-commit-sha"})
    )


@respx.mock
def test_commit_files_updates_existing_branch() -> None:
    _mock_blob_tree_commit()
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/announce-ns-pkg").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "base-sha"}})
    )
    respx.patch("https://api.github.com/repos/ocx-sh/index/git/refs/heads/announce-ns-pkg").mock(
        return_value=httpx.Response(200, json={})
    )

    result = _client().commit_files(
        branch="announce-ns-pkg",
        base_sha="base-sha",
        message="regenerate ns/pkg",
        files={"p/ns/pkg.json": b"{}", "p/ns/pkg/o/sha256/deadbeef.json": None},
    )

    assert result == "new-commit-sha"


@respx.mock
def test_commit_files_creates_missing_branch() -> None:
    _mock_blob_tree_commit()
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/announce-ns-pkg").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    respx.post("https://api.github.com/repos/ocx-sh/index/git/refs").mock(
        return_value=httpx.Response(201, json={"ref": "refs/heads/announce-ns-pkg"})
    )

    result = _client().commit_files(
        branch="announce-ns-pkg",
        base_sha="base-sha",
        message="regenerate ns/pkg",
        files={"p/ns/pkg.json": b"{}"},
    )

    assert result == "new-commit-sha"


@respx.mock
def test_commit_files_stale_base_sha_raises_transient() -> None:
    _mock_blob_tree_commit()
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/announce-ns-pkg").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "someone-elses-sha"}})
    )
    respx.patch("https://api.github.com/repos/ocx-sh/index/git/refs/heads/announce-ns-pkg").mock(
        return_value=httpx.Response(422, json={"message": "not a fast-forward"})
    )

    with pytest.raises(TransientError, match="moved since base_sha"):
        _client().commit_files(
            branch="announce-ns-pkg",
            base_sha="base-sha",
            message="regenerate ns/pkg",
            files={"p/ns/pkg.json": b"{}"},
        )


@respx.mock
def test_commit_files_missing_base_sha_raises_transient() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/git/commits/ghost-sha").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    with pytest.raises(TransientError, match="base_sha 'ghost-sha' not found"):
        _client().commit_files(
            branch="announce-ns-pkg",
            base_sha="ghost-sha",
            message="regenerate ns/pkg",
            files={"p/ns/pkg.json": b"{}"},
        )


@respx.mock
def test_commit_files_branch_created_concurrently_raises_transient() -> None:
    _mock_blob_tree_commit()
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/announce-ns-pkg").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    respx.post("https://api.github.com/repos/ocx-sh/index/git/refs").mock(
        return_value=httpx.Response(422, json={"message": "Reference already exists"})
    )

    with pytest.raises(TransientError, match="created concurrently"):
        _client().commit_files(
            branch="announce-ns-pkg",
            base_sha="base-sha",
            message="regenerate ns/pkg",
            files={"p/ns/pkg.json": b"{}"},
        )


# ---- open_or_update_pull_request --------------------------------------------


@respx.mock
def test_open_or_update_pull_request_creates_when_none_exists() -> None:
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/pulls",
        params={"head": "ocx-sh:announce-ns-pkg", "base": "main", "state": "open"},
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.post("https://api.github.com/repos/ocx-sh/index/pulls").mock(
        return_value=httpx.Response(201, json={"number": 42})
    )

    result = _client().open_or_update_pull_request(
        branch="announce-ns-pkg", base="main", title="regen ns/pkg", body="body"
    )

    assert result == 42


@respx.mock
def test_open_or_update_pull_request_updates_existing_when_changed() -> None:
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/pulls",
        params={"head": "ocx-sh:announce-ns-pkg", "base": "main", "state": "open"},
    ).mock(
        return_value=httpx.Response(
            200, json=[{"number": 7, "title": "old title", "body": "old body"}]
        )
    )
    respx.patch("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json={"number": 7})
    )

    result = _client().open_or_update_pull_request(
        branch="announce-ns-pkg", base="main", title="new title", body="new body"
    )

    assert result == 7


@respx.mock
def test_open_or_update_pull_request_no_op_when_unchanged() -> None:
    # No PATCH route registered at all — if the adapter tried to PATCH,
    # respx would raise for an unmocked request, failing the test.
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/pulls",
        params={"head": "ocx-sh:announce-ns-pkg", "base": "main", "state": "open"},
    ).mock(
        return_value=httpx.Response(
            200, json=[{"number": 7, "title": "same title", "body": "same body"}]
        )
    )

    result = _client().open_or_update_pull_request(
        branch="announce-ns-pkg", base="main", title="same title", body="same body"
    )

    assert result == 7


@respx.mock
def test_open_or_update_pull_request_cross_repo_head_repo() -> None:
    # `cli/announce.py`'s `--fork` mode: the PR is opened against the index
    # repo (`_client()`'s own owner/repo) with a fork-owner-qualified head —
    # never `self.owner`.
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/pulls",
        params={"head": "alice:announce-ns-pkg", "base": "main", "state": "open"},
    ).mock(return_value=httpx.Response(200, json=[]))
    create_route = respx.post("https://api.github.com/repos/ocx-sh/index/pulls").mock(
        return_value=httpx.Response(201, json={"number": 42})
    )

    result = _client().open_or_update_pull_request(
        branch="announce-ns-pkg",
        base="main",
        title="regen ns/pkg",
        body="body",
        head_repo="alice/fork",
    )

    assert result == 42
    assert json.loads(create_route.calls.last.request.content)["head"] == "alice:announce-ns-pkg"


@respx.mock
def test_open_or_update_pull_request_refuses_a_non_list_lookup_body() -> None:
    """`_find_open_pull_request`'s `GET .../pulls?head=...` is a single,
    unpaginated read, the same shape as `find_pull_request_by_head_sha` —
    same guard needed, same failure mode (`matches[0]` on a dict's keys)
    without it."""
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/pulls",
        params={"head": "ocx-sh:announce-ns-pkg", "base": "main", "state": "open"},
    ).mock(return_value=httpx.Response(200, json={"message": "unexpected"}))

    with pytest.raises(ForgeError, match="non-list body"):
        _client().open_or_update_pull_request(
            branch="announce-ns-pkg", base="main", title="regen ns/pkg", body="body"
        )


# ---- add_labels ---------------------------------------------------------------


@respx.mock
def test_add_labels_success() -> None:
    route = respx.post("https://api.github.com/repos/ocx-sh/index/issues/7/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "refresh"}])
    )

    _client().add_labels(7, ["refresh"])

    assert route.called


# ---- remove_label -------------------------------------------------------------


@respx.mock
def test_remove_label_success() -> None:
    route = respx.delete(
        "https://api.github.com/repos/ocx-sh/index/issues/7/labels/human-review-required"
    ).mock(return_value=httpx.Response(200, json=[{"name": "refresh"}]))

    _client().remove_label(7, "human-review-required")

    assert route.called


@respx.mock
def test_remove_label_tolerates_a_label_that_is_not_there() -> None:
    """404 answers both "not on this pull request" and "not in this
    repository", and the caller wanted neither of them to be there. Raising
    would end a governance sweep over a label that is already absent."""
    respx.delete(
        "https://api.github.com/repos/ocx-sh/index/issues/7/labels/human-review-required"
    ).mock(return_value=httpx.Response(404, json={"message": "Label does not exist"}))

    _client().remove_label(7, "human-review-required")


@respx.mock
def test_remove_label_raises_on_a_real_failure() -> None:
    """Only 404 is swallowed. A 422 means the request was wrong, and a sweep
    that hid it would leave the stale label in place with nothing said."""
    respx.delete(
        "https://api.github.com/repos/ocx-sh/index/issues/7/labels/human-review-required"
    ).mock(return_value=httpx.Response(422, json={"message": "Validation Failed"}))

    with pytest.raises(ForgeError):
        _client().remove_label(7, "human-review-required")


# ---- enable_auto_merge --------------------------------------------------------


@respx.mock
def test_enable_auto_merge_success() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json={"node_id": "PR_kwabc"})
    )
    respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(200, json={"data": {"enablePullRequestAutoMerge": {}}})
    )

    _client().enable_auto_merge(7, head_sha="c" * 40)  # no exception = success


@respx.mock
def test_enable_auto_merge_graphql_error_payload_raises() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json={"node_id": "PR_kwabc"})
    )
    respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": None,
                "errors": [{"message": "Pull request Auto merge is not allowed"}],
            },
        )
    )

    with pytest.raises(GraphQLError, match="Pull request Auto merge is not allowed"):
        _client().enable_auto_merge(7, head_sha="c" * 40)


@respx.mock
def test_a_moved_head_does_not_fail_the_auto_merge_arm() -> None:
    """`expectedHeadOid` binds the arm to the revision that was gated. GitHub
    reports a mismatch as a GraphQL error message rather than a status code,
    so the message is what has to be read. A moved head means the decision was
    about a revision that is no longer current — the next run gates the new
    one — and raising here would take a whole sweep down over a race."""
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json={"node_id": "PR_kwabc"})
    )
    respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": None,
                "errors": [{"message": "Expected head oid did not match the current head"}],
            },
        )
    )

    _client().enable_auto_merge(7, head_sha="c" * 40)  # no exception = handled


@respx.mock
def test_the_auto_merge_arm_names_the_gated_revision() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json={"node_id": "PR_kwabc"})
    )
    graphql = respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(200, json={"data": {"enablePullRequestAutoMerge": {}}})
    )

    _client().enable_auto_merge(7, head_sha="c" * 40)

    request = cast("httpx.Request", cast("list[Any]", graphql.calls)[0].request)
    variables = cast("dict[str, Any]", json.loads(request.content))["variables"]
    assert variables["expectedHeadOid"] == "c" * 40


# ---- enable_auto_merge: the already-mergeable fallback -------------------------


def _already_mergeable(message: str = "Pull request is in clean status") -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json={"node_id": "PR_kwabc"})
    )
    respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(200, json={"data": None, "errors": [{"message": message}]})
    )


@respx.mock
@pytest.mark.parametrize(
    "message",
    ["Pull request is in clean status", "Pull request is in unstable status"],
)
def test_an_already_mergeable_pull_request_is_squash_merged_instead(message: str) -> None:
    """Arming is only possible while the merge is still BLOCKED. When every
    required check finishes before the arm call gets there, GitHub refuses the
    mutation outright — and a machine-lane PR would then sit open forever
    waiting for an auto-merge nobody armed. Both refusal spellings mean the
    same thing and both must merge."""
    _already_mergeable(message)
    merge = respx.put("https://api.github.com/repos/ocx-sh/index/pulls/7/merge").mock(
        return_value=httpx.Response(200, json={"merged": True})
    )

    _client().enable_auto_merge(7, head_sha="c" * 40)

    request = cast("httpx.Request", cast("list[Any]", merge.calls)[0].request)
    payload = cast("dict[str, Any]", json.loads(request.content))
    assert payload == {"merge_method": "squash", "sha": "c" * 40}


@respx.mock
def test_the_fallback_merge_is_pinned_to_the_gated_revision() -> None:
    """`sha` is the REST spelling of `gh pr merge --match-head-commit`, and it
    is the same guard `expectedHeadOid` was on the arming path: a push racing
    this call answers 409 rather than merging a revision nothing gated. That
    409 returns quietly, because a stale decision is not an error — the event
    for the new head re-gates it."""
    _already_mergeable()
    respx.put("https://api.github.com/repos/ocx-sh/index/pulls/7/merge").mock(
        return_value=httpx.Response(409, json={"message": "Head branch was modified"})
    )

    _client().enable_auto_merge(7, head_sha="c" * 40)  # no exception = handled


@respx.mock
def test_the_fallback_merge_still_raises_when_the_pull_request_is_not_mergeable() -> None:
    """Only the head-moved race is swallowed. A 405 ("not mergeable") is a
    machine lane that has genuinely stalled and must go red rather than
    reporting a merge that never happened."""
    _already_mergeable()
    respx.put("https://api.github.com/repos/ocx-sh/index/pulls/7/merge").mock(
        return_value=httpx.Response(405, json={"message": "Pull Request is not mergeable"})
    )

    with pytest.raises(ForgeError):
        _client().enable_auto_merge(7, head_sha="c" * 40)


# ---- withdraw_auto_merge -------------------------------------------------------


@respx.mock
def test_withdraw_auto_merge_is_a_noop_when_not_armed() -> None:
    """The REST pull-request object's own `auto_merge` field is `null` when
    it is off — the ordinary human-lane case. No GraphQL route is mocked, so
    a call here would fail loudly if the disable mutation fired anyway."""
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json={"node_id": "PR_kwabc", "auto_merge": None})
    )

    _client().withdraw_auto_merge(7)  # no exception, no GraphQL call


@respx.mock
def test_withdraw_auto_merge_disables_when_armed() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(
            200, json={"node_id": "PR_kwabc", "auto_merge": {"enabled_by": {"login": "alice"}}}
        )
    )
    graphql = respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(200, json={"data": {"disablePullRequestAutoMerge": {}}})
    )

    _client().withdraw_auto_merge(7)  # no exception = success

    request = cast("httpx.Request", cast("list[Any]", graphql.calls)[0].request)
    variables = cast("dict[str, Any]", json.loads(request.content))["variables"]
    assert variables == {"pullRequestId": "PR_kwabc"}


@respx.mock
def test_withdraw_auto_merge_graphql_error_payload_raises() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(
            200, json={"node_id": "PR_kwabc", "auto_merge": {"enabled_by": {"login": "alice"}}}
        )
    )
    respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": None, "errors": [{"message": "Resource not accessible"}]},
        )
    )

    with pytest.raises(GraphQLError, match="Resource not accessible"):
        _client().withdraw_auto_merge(7)


# ---- get_pull_request_info ----------------------------------------------------


def _pr_payload(head_sha: str) -> dict[str, Any]:
    """A minimal `GET /pulls/{n}` body, parameterised on the one field the
    head-movement re-read compares."""
    return {
        "base": {"sha": "base-sha"},
        "head": {"sha": head_sha},
        "user": {"login": "alice", "id": 1},
        "updated_at": "2026-07-17T00:00:00Z",
        "labels": [],
    }


@respx.mock
def test_get_pull_request_info_success() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "base": {"sha": "base-sha"},
                "head": {"sha": "head-sha"},
                "user": {"login": "alice", "id": 1},
                "updated_at": "2026-07-17T00:00:00Z",
                "labels": [{"name": "checks-failed"}, {"name": "refresh"}],
            },
        )
    )
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/pulls/7/files",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[{"filename": "p/ns/pkg.json"}]))

    result = _client().get_pull_request_info(7)

    assert result == PullRequestInfo(
        number=7,
        base_sha="base-sha",
        head_sha="head-sha",
        changed_paths=("p/ns/pkg.json",),
        author_login="alice",
        author_id=1,
        updated_at="2026-07-17T00:00:00Z",
        labels=("checks-failed", "refresh"),
    )


@respx.mock
def test_get_pull_request_info_refuses_a_non_object_body() -> None:
    """`_pull_request` used to `cast` the decoded body with nothing proving
    it was ever a JSON object — a forge answering with a list or a bare
    scalar would flow straight into `payload["base"]["sha"]` far from here."""
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json=["unexpected"])
    )

    with pytest.raises(ForgeError, match="non-object body"):
        _client().get_pull_request_info(7)


@respx.mock
def test_get_pull_request_info_missing_raises_keyerror() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    with pytest.raises(KeyError, match="no such pull request: #999"):
        _client().get_pull_request_info(999)


@respx.mock
def test_get_pull_request_info_paginates_changed_files() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "base": {"sha": "base-sha"},
                "head": {"sha": "head-sha"},
                "user": {"login": "alice", "id": 1},
                "updated_at": "2026-07-17T00:00:00Z",
                "labels": [],
            },
        )
    )
    page_1_url = "https://api.github.com/repos/ocx-sh/index/pulls/7/files"
    page_2_url = "https://api.github.com/repos/ocx-sh/index/pulls/7/files?page=2"
    respx.get(page_1_url, params={"per_page": "100"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"filename": "p/a/a.json"}],
            headers={"Link": f'<{page_2_url}>; rel="next"'},
        )
    )
    respx.get(page_2_url).mock(return_value=httpx.Response(200, json=[{"filename": "p/b/b.json"}]))

    result = _client().get_pull_request_info(7)

    assert result.changed_paths == ("p/a/a.json", "p/b/b.json")


@respx.mock
def test_a_push_during_the_file_walk_refuses_the_read() -> None:
    """The head sha and the changed paths come from two endpoints, and a
    paginated file walk can span many round trips. A force-push A -> B -> A
    across that window hands back B's file list under A's sha.

    Nothing downstream can catch it: `cli/classify_pr.py` classifies content
    that is not at the head, and `cli/governance_gate.py` then arms
    auto-merge bound to `head_sha` — which still resolves, so GitHub's own
    `expectedHeadOid` guard passes. Refusing here is the only place the two
    facts can be tied together, and `TransientError` is right because the
    next tick simply re-reads.

    The pull-request route below answers `head-sha` first and `pushed-sha`
    second, which is exactly what the adapter's re-read sees.
    """
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        side_effect=[
            httpx.Response(200, json=_pr_payload("head-sha")),
            httpx.Response(200, json=_pr_payload("pushed-sha")),
        ]
    )
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/pulls/7/files",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[{"filename": "p/ns/pkg.json"}]))

    with pytest.raises(TransientError, match="was pushed to while its changed files were read"):
        _client().get_pull_request_info(7)


@respx.mock
def test_an_unchanged_head_across_the_file_walk_is_accepted() -> None:
    """The complementary half — the re-read must not turn every ordinary read
    into a retry. Same route, same sha both times."""
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json=_pr_payload("head-sha"))
    )
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/pulls/7/files",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[{"filename": "p/ns/pkg.json"}]))

    assert _client().get_pull_request_info(7).head_sha == "head-sha"


# ---- set_commit_status ---------------------------------------------------------


@respx.mock
def test_set_commit_status_success() -> None:
    respx.post("https://api.github.com/repos/ocx-sh/index/statuses/sha123").mock(
        return_value=httpx.Response(201, json={"state": "success"})
    )

    _client().set_commit_status(
        "sha123",
        context="governance/review-required",
        state="success",
        description="clean refresh",
    )


# ---- request_reviewers (G-20) --------------------------------------------------


@respx.mock
def test_request_reviewers_success() -> None:
    route = respx.post(
        "https://api.github.com/repos/ocx-sh/index/pulls/7/requested_reviewers"
    ).mock(return_value=httpx.Response(201, json={"number": 7}))

    _client().request_reviewers(7, ["alice", "bob"])

    assert json.loads(route.calls.last.request.content) == {"reviewers": ["alice", "bob"]}


# ---- create_comment (G-20, idempotent via hidden marker) -----------------------

_MARKER = "<!-- indexbot:governance -->"


_SELF_ID = 1001


def _self_user() -> respx.Route:
    """`GET /user` — how the adapter learns which comments are its own. A
    personal/organization access token answers it; the installation token a
    workflow's `GITHUB_TOKEN` is does not (see `_self_user_forbidden`)."""
    return respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"id": _SELF_ID, "login": "indexbot"})
    )


def _self_user_forbidden() -> respx.Route:
    """What `GET /user` answers an installation token — the ordinary CI case,
    not an error."""
    return respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by integration"})
    )


def _comment(
    cid: int,
    body: str,
    *,
    association: str = "OWNER",
    user_type: str = "User",
    user_id: int = _SELF_ID,
) -> dict[str, Any]:
    return {
        "id": cid,
        "body": body,
        "author_association": association,
        "user": {"login": "alice", "type": user_type, "id": user_id},
    }


@respx.mock
def test_create_comment_creates_when_no_marked_comment_exists() -> None:
    _self_user()
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[]))
    create_route = respx.post("https://api.github.com/repos/ocx-sh/index/issues/7/comments").mock(
        return_value=httpx.Response(201, json={"id": 99})
    )

    _client().create_comment(7, f"{_MARKER}\nreview required", marker=_MARKER)

    assert create_route.called


@respx.mock
def test_create_comment_creates_when_comments_exist_but_none_marked() -> None:
    # A comment list with entries, none carrying the marker, exercises the
    # "keep scanning past a non-matching comment" loop path before falling
    # through to "create a new one".
    _self_user()
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[{"id": 1, "body": "unrelated comment"}]))
    create_route = respx.post("https://api.github.com/repos/ocx-sh/index/issues/7/comments").mock(
        return_value=httpx.Response(201, json={"id": 99})
    )

    _client().create_comment(7, f"{_MARKER}\nreview required", marker=_MARKER)

    assert create_route.called


@respx.mock
def test_create_comment_updates_when_marked_comment_differs() -> None:
    _self_user()
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[_comment(99, f"{_MARKER}\nold state")]))
    update_route = respx.patch("https://api.github.com/repos/ocx-sh/index/issues/comments/99").mock(
        return_value=httpx.Response(200, json={"id": 99})
    )

    _client().create_comment(7, f"{_MARKER}\nnew state", marker=_MARKER)

    assert update_route.called


@respx.mock
def test_create_comment_no_op_when_marked_comment_unchanged() -> None:
    # No PATCH route registered — a PATCH attempt would fail as unmocked.
    _self_user()
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[_comment(99, f"{_MARKER}\nsame state")]))

    _client().create_comment(7, f"{_MARKER}\nsame state", marker=_MARKER)


@respx.mock
def test_the_marker_only_counts_on_a_comment_from_this_side() -> None:
    """The marker ships in every notice this bot posts, so a pull request's
    own author can copy it into a comment of their own. Editing *that* comment
    instead of posting the notice would let the author choose what the
    governance notice says."""
    _self_user()
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                _comment(
                    99, f"{_MARKER}\nlooks fine to me", association="CONTRIBUTOR", user_id=4242
                )
            ],
        )
    )
    create_route = respx.post("https://api.github.com/repos/ocx-sh/index/issues/7/comments").mock(
        return_value=httpx.Response(201, json={"id": 100})
    )

    _client().create_comment(7, f"{_MARKER}\nreview required", marker=_MARKER)

    assert create_route.called, "the bot posts its own notice instead"


@respx.mock
def test_an_org_member_or_read_only_collaborator_cannot_hold_the_marked_comment() -> None:
    """The filter this replaced accepted `author_association in {OWNER, MEMBER,
    COLLABORATOR}` under the claim that those mean "write access here". They
    do not. `MEMBER` is *organization* membership — on a public org that can
    be self-service, and it grants nothing on this repository; `COLLABORATOR`
    covers the read- and triage-only roles too. Either could post the public
    marker and have `create_comment` adopt and edit that comment, choosing
    what the governance notice says. The identity check is now exact: the
    comment is this token's or it is not.

    Both entries below carry a repo-side-looking association and somebody
    else's user id. Reverting to the association check makes this PATCH a
    stranger's comment, and no PATCH route is registered to absorb that.
    """
    _self_user()
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                _comment(98, f"{_MARKER}\nmerge it", association="MEMBER", user_id=4242),
                _comment(99, f"{_MARKER}\nmerge it", association="COLLABORATOR", user_id=4243),
            ],
        )
    )
    create_route = respx.post("https://api.github.com/repos/ocx-sh/index/issues/7/comments").mock(
        return_value=httpx.Response(201, json={"id": 100})
    )

    _client().create_comment(7, f"{_MARKER}\nreview required", marker=_MARKER)

    assert create_route.called, "the bot posts its own notice instead"


@respx.mock
def test_this_tokens_own_comment_is_adopted_and_edited() -> None:
    """The other half: an exact identity match is what keeps `create_comment`
    idempotent (G-20 — one notice per PR across repeated runs, never a fresh
    comment each time). The association here is `NONE`, which the replaced
    filter would have rejected outright."""
    _self_user()
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(
        return_value=httpx.Response(
            200, json=[_comment(99, f"{_MARKER}\nold", association="NONE", user_id=_SELF_ID)]
        )
    )
    update_route = respx.patch("https://api.github.com/repos/ocx-sh/index/issues/comments/99").mock(
        return_value=httpx.Response(200, json={"id": 99})
    )

    _client().create_comment(7, f"{_MARKER}\nnew", marker=_MARKER)

    assert update_route.called


@respx.mock
def test_an_installation_token_falls_back_to_a_bot_only_filter() -> None:
    """A workflow's `GITHUB_TOKEN` is an installation token: `GET /user`
    answers it `403 Resource not accessible by integration`, so the exact
    identity is simply not available. That is the ordinary CI case, not an
    error, and the run must still find the notice it posted last tick.

    The fallback is the strongest signal an outside contributor cannot
    satisfy — `type: "Bot"` is not claimable by an ordinary account, and an
    App can only comment where it is installed, which takes admin. It is
    *not* the exact test; `_is_repo_side_author` states that residual. What
    it must never admit is the `CONTRIBUTOR`/`User` comment below, which any
    fork author can post.
    """
    _self_user_forbidden()
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                _comment(98, f"{_MARKER}\nmerge it", association="CONTRIBUTOR", user_id=4242),
                _comment(
                    99, f"{_MARKER}\nold", association="NONE", user_type="Bot", user_id=1234567
                ),
            ],
        )
    )
    update_route = respx.patch("https://api.github.com/repos/ocx-sh/index/issues/comments/99").mock(
        return_value=httpx.Response(200, json={"id": 99})
    )

    _client().create_comment(7, f"{_MARKER}\nnew", marker=_MARKER)

    assert update_route.called, "the App identity's own comment is the one adopted"


@respx.mock
def test_an_anonymous_adapter_asks_no_identity_question() -> None:
    """`announce --out` runs tokenless against public reads; `GET /user` would
    401 there, and there is nothing to identify. No route is registered for
    it, so a call would fail loudly."""
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[_comment(99, f"{_MARKER}\nhi")]))

    anonymous = GitHubApi(owner="ocx-sh", repo="index")

    assert anonymous._find_marked_comment(7, _MARKER) is None  # pyright: ignore[reportPrivateUsage]


@respx.mock
def test_the_token_identity_is_resolved_once_per_instance() -> None:
    """`GET /user` cannot change inside a run, and a governance sweep asks the
    marker question once per open pull request."""
    user = _self_user()
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues/7/comments",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.post("https://api.github.com/repos/ocx-sh/index/issues/7/comments").mock(
        return_value=httpx.Response(201, json={"id": 100})
    )

    client = _client()
    client.create_comment(7, f"{_MARKER}\na", marker=_MARKER)
    client.create_comment(7, f"{_MARKER}\nb", marker=_MARKER)

    assert user.call_count == 1


# ---- create_or_update_issue (promoted onto ForgePort) -------------------------


@respx.mock
def test_create_or_update_issue_creates_when_no_match() -> None:
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues",
        params={"state": "open", "per_page": "100"},
    ).mock(
        # A same-list non-PR issue with a different title exercises the
        # "keep scanning past a non-matching item" loop path.
        return_value=httpx.Response(
            200, json=[{"number": 5, "title": "Anomaly: other/pkg", "body": "unrelated"}]
        )
    )
    respx.post("https://api.github.com/repos/ocx-sh/index/issues").mock(
        return_value=httpx.Response(201, json={"number": 11})
    )

    result = _client().create_or_update_issue(
        title="Anomaly: ns/pkg", body="details", labels=["anomaly"]
    )

    assert result == 11


@respx.mock
def test_create_or_update_issue_updates_when_body_changed() -> None:
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues",
        params={"state": "open", "per_page": "100"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"number": 3, "title": "unrelated pr", "pull_request": {}},
                {"number": 11, "title": "Anomaly: ns/pkg", "body": "old details"},
            ],
        )
    )
    respx.patch("https://api.github.com/repos/ocx-sh/index/issues/11").mock(
        return_value=httpx.Response(200, json={"number": 11})
    )

    result = _client().create_or_update_issue(title="Anomaly: ns/pkg", body="new details")

    assert result == 11


@respx.mock
def test_create_or_update_issue_no_op_when_body_unchanged() -> None:
    # No PATCH route registered — a PATCH attempt would fail as unmocked.
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/issues",
        params={"state": "open", "per_page": "100"},
    ).mock(
        return_value=httpx.Response(
            200, json=[{"number": 11, "title": "Anomaly: ns/pkg", "body": "same details"}]
        )
    )

    result = _client().create_or_update_issue(title="Anomaly: ns/pkg", body="same details")

    assert result == 11


# ---- find_pull_request_by_head_sha (WP5-C, ADR-6 FP-8) ------------------------


@respx.mock
def test_find_pull_request_by_head_sha_exact_match_same_repo_is_not_a_fork() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/commits/deadbeef/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 9,
                    "head": {"sha": "deadbeef", "repo": {"full_name": "ocx-sh/index"}},
                }
            ],
        )
    )

    result = _client().find_pull_request_by_head_sha("deadbeef")

    assert result == PullRequestHeadMatch(number=9, is_fork=False)


@respx.mock
def test_find_pull_request_by_head_sha_exact_match_fork_is_a_fork() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/commits/deadbeef/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 9,
                    "head": {"sha": "deadbeef", "repo": {"full_name": "mallory/index"}},
                }
            ],
        )
    )

    result = _client().find_pull_request_by_head_sha("deadbeef")

    assert result == PullRequestHeadMatch(number=9, is_fork=True)


@respx.mock
def test_find_pull_request_by_head_sha_null_head_repo_is_treated_as_not_fork() -> None:
    """A deleted fork reports `head.repo: null` — treated as "same repo" (not
    in FP-8's fork-only scope), matching `pr-checks-label.yml`'s own
    `[ -z "$head_repo" ] || [ "$head_repo" = "$REPO" ]` check."""
    respx.get("https://api.github.com/repos/ocx-sh/index/commits/deadbeef/pulls").mock(
        return_value=httpx.Response(
            200, json=[{"number": 9, "head": {"sha": "deadbeef", "repo": None}}]
        )
    )

    result = _client().find_pull_request_by_head_sha("deadbeef")

    assert result == PullRequestHeadMatch(number=9, is_fork=False)


@respx.mock
def test_find_pull_request_by_head_sha_ignores_a_pr_whose_head_moved_on() -> None:
    """The head-sha filter, not "first result": a PR returned by this
    endpoint may list `deadbeef` in its history without it being the PR's
    CURRENT head any more — exactly the case a rebase/force-push produces."""
    respx.get("https://api.github.com/repos/ocx-sh/index/commits/deadbeef/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 9,
                    "head": {"sha": "newer-sha", "repo": {"full_name": "mallory/index"}},
                }
            ],
        )
    )

    assert _client().find_pull_request_by_head_sha("deadbeef") is None


@respx.mock
def test_find_pull_request_by_head_sha_no_association_returns_none() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/commits/deadbeef/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )

    assert _client().find_pull_request_by_head_sha("deadbeef") is None


@respx.mock
def test_find_pull_request_by_head_sha_unknown_commit_returns_none() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/commits/deadbeef/pulls").mock(
        return_value=httpx.Response(404, json={"message": "No commit found"})
    )

    assert _client().find_pull_request_by_head_sha("deadbeef") is None


@respx.mock
def test_find_pull_request_by_head_sha_refuses_a_non_list_body() -> None:
    """This endpoint is a single, unpaginated `GET` — it never routes through
    `paginate`'s own `_as_object_list` guard, so it needs the same check
    applied directly or a body shaped like an error object flows into
    `for item in items` as if it were already validated."""
    respx.get("https://api.github.com/repos/ocx-sh/index/commits/deadbeef/pulls").mock(
        return_value=httpx.Response(200, json={"message": "unexpected"})
    )

    with pytest.raises(ForgeError, match="non-list body"):
        _client().find_pull_request_by_head_sha("deadbeef")


# ---- close_pull_request (WP5-C) ------------------------------------------------


@respx.mock
def test_close_pull_request_patches_state_closed() -> None:
    patch = respx.patch("https://api.github.com/repos/ocx-sh/index/pulls/9").mock(
        return_value=httpx.Response(200, json={"number": 9, "state": "closed"})
    )

    _client().close_pull_request(9)

    assert json.loads(patch.calls.last.request.content) == {"state": "closed"}


# ---- list_approvals ----------------------------------------------------------


@respx.mock
def test_list_approvals_keeps_only_approvals_of_the_current_head() -> None:
    """A review is recorded against the commit it was left on. An approval of
    an earlier push is not an approval of what would merge now — the release
    means a person read *these* bytes."""
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "user": {"login": "carol", "id": 99},
                    "state": "APPROVED",
                    "commit_id": "head-sha",
                },
                {
                    "user": {"login": "dave", "id": 7},
                    "state": "APPROVED",
                    "commit_id": "older-push",
                },
                {
                    "user": {"login": "erin", "id": 8},
                    "state": "CHANGES_REQUESTED",
                    "commit_id": "head-sha",
                },
                {
                    "user": {"login": "carol", "id": 99},
                    "state": "COMMENTED",
                    "commit_id": "head-sha",
                },
            ],
        )
    )

    assert _client().list_approvals(7, head_sha="head-sha") == (99,)


@respx.mock
def test_an_approval_is_reported_by_numeric_id_never_by_login() -> None:
    """An approval outranks every disposition the gate can reach, including
    `governance.auto_merge = never`, so what it returns is the thing
    `cli/governance_check.py` matches against `.github/maintainers.yml`.

    A GitHub login is renameable and, once released, claimable by a stranger;
    `user.id` is not. Reporting the login here would carry a former
    maintainer's veto to whoever holds their old name — which is why
    `owners[].github_id` exists in the first place. The payload below gives
    both fields, so a revert to `user.login` still parses and this assertion
    is what catches it.
    """
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"user": {"login": "carol", "id": 99}, "state": "APPROVED", "commit_id": "head-sha"}
            ],
        )
    )

    assert _client().list_approvals(7, head_sha="head-sha") == (99,)


# ---- list_open_pull_requests -------------------------------------------------


@respx.mock
def test_list_open_pull_requests_returns_ascending_numbers() -> None:
    """Implemented on GitHub too, not only on GitLab: it is what lets the
    poll lane be exercised against either forge, and what a GitHub deployment
    would use for a sweep that re-gates PRs whose base moved."""
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls").mock(
        return_value=httpx.Response(200, json=[{"number": 9}, {"number": 2}])
    )

    assert _client().list_open_pull_requests() == (2, 9)


# ---- _paginate bound (shared by get_pull_request_info / create_or_update_issue) --


@respx.mock
def test_paginate_exceeds_page_cap_raises_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ocx_indexbot.adapters._http.MAX_PAGES", 2)
    url = "https://api.github.com/repos/ocx-sh/index/issues"
    # A next-link that always points back at itself never terminates —
    # proves the hard page cap (not an unbounded loop) is what stops it.
    respx.get(url).mock(
        return_value=httpx.Response(200, json=[], headers={"Link": f'<{url}>; rel="next"'})
    )

    with pytest.raises(TransientError, match="pagination exceeded 2 pages"):
        _client().create_or_update_issue(title="Anomaly: ns/pkg", body="details")


@respx.mock
def test_a_next_link_pointing_off_the_api_host_is_refused() -> None:
    """The write-scoped token rides in the client's default headers, so it
    goes wherever the client is pointed. A `Link: rel="next"` is response
    data: a compromised or spoofed forge response naming another host would
    otherwise hand the token to it. Same-origin or no follow."""
    url = "https://api.github.com/repos/ocx-sh/index/issues"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={"Link": '<https://evil.example/collect?page=2>; rel="next"'},
        )
    )

    with pytest.raises(ForgeError, match="left the API host"):
        _client().create_or_update_issue(title="Anomaly: ns/pkg", body="details")


@respx.mock
def test_a_list_endpoint_answering_with_an_object_is_refused() -> None:
    """`list.extend` is indiscriminate: given a JSON object it appends that
    object's keys, so the walk returns `list[str]` under a
    `list[dict[str, Any]]` annotation and the first `item["id"]` fails far
    away with "string indices must be integers". Refuse it at the boundary,
    where the body that caused it is still in hand."""
    url = "https://api.github.com/repos/ocx-sh/index/issues"
    respx.get(url).mock(return_value=httpx.Response(200, json={"message": "Not Found"}))

    with pytest.raises(ForgeError, match="non-list body"):
        _client().create_or_update_issue(title="Anomaly: ns/pkg", body="details")


@respx.mock
def test_a_list_endpoint_answering_with_scalars_is_refused() -> None:
    """The array shape alone is not enough — every element is indexed by key
    downstream, so a list of scalars fails the same way one step later."""
    url = "https://api.github.com/repos/ocx-sh/index/issues"
    respx.get(url).mock(return_value=httpx.Response(200, json=["not", "objects"]))

    with pytest.raises(ForgeError, match="non-list body"):
        _client().create_or_update_issue(title="Anomaly: ns/pkg", body="details")


@respx.mock
def test_a_connect_error_is_transient_not_an_unhandled_exception() -> None:
    """The GitHub half of the same rule.

    `adapters/registry_v2.py` has caught `httpx.TransportError` since it was
    written; neither forge adapter did, so a timeout or a reset talking to a
    forge left the run with exit 1 and a traceback instead of the retryable
    75. Both now build their client through `_http.client`, which is where the
    mapping lives — one place, rather than a rule to remember at each of the
    ~37 call sites between them.
    """
    respx.get("https://api.github.com/repos/ocx-sh/index/git/ref/heads/main").mock(
        side_effect=httpx.ConnectError("connection reset by peer")
    )

    with pytest.raises(TransientError) as caught:
        _client().get_ref_sha("main")

    assert caught.value.exit_code is ExitCode.TRANSIENT
    assert "ConnectError" in str(caught.value)
