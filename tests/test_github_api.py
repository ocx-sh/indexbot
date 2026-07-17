"""`adapters/github_api.py` — respx route mocks (CONTRACTS.md §2, §10).

One `respx.mock` route per distinct response class per method. Assertions
target the port-level return value/exception only, never respx call
internals, so this suite survives an adapter refactor (CONTRACTS.md §2).
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from indexbot.adapters.github_api import GitHubApi, GraphQLError
from indexbot.errors import TransientError
from indexbot.model import PullRequestInfo

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

    with pytest.raises(httpx.HTTPStatusError):
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


# ---- add_labels ---------------------------------------------------------------


@respx.mock
def test_add_labels_success() -> None:
    route = respx.post("https://api.github.com/repos/ocx-sh/index/issues/7/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "refresh"}])
    )

    _client().add_labels(7, ["refresh"])

    assert route.called


# ---- enable_auto_merge --------------------------------------------------------


@respx.mock
def test_enable_auto_merge_success() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(200, json={"node_id": "PR_kwabc"})
    )
    respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(200, json={"data": {"enablePullRequestAutoMerge": {}}})
    )

    _client().enable_auto_merge(7)  # no exception = success


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
        _client().enable_auto_merge(7)


# ---- get_pull_request_info ----------------------------------------------------


@respx.mock
def test_get_pull_request_info_success() -> None:
    respx.get("https://api.github.com/repos/ocx-sh/index/pulls/7").mock(
        return_value=httpx.Response(
            200,
            json={"base": {"sha": "base-sha"}, "head": {"sha": "head-sha"}},
        )
    )
    respx.get(
        "https://api.github.com/repos/ocx-sh/index/pulls/7/files",
        params={"per_page": "100"},
    ).mock(return_value=httpx.Response(200, json=[{"filename": "p/ns/pkg.json"}]))

    result = _client().get_pull_request_info(7)

    assert result == PullRequestInfo(
        number=7, base_sha="base-sha", head_sha="head-sha", changed_paths=("p/ns/pkg.json",)
    )


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
            json={"base": {"sha": "base-sha"}, "head": {"sha": "head-sha"}},
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


# ---- create_or_update_issue (extra capability, not on GitHubPort) --------------


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


# ---- _paginate bound (shared by get_pull_request_info / create_or_update_issue) --


@respx.mock
def test_paginate_exceeds_page_cap_raises_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("indexbot.adapters.github_api._MAX_PAGES", 2)
    url = "https://api.github.com/repos/ocx-sh/index/issues"
    # A next-link that always points back at itself never terminates —
    # proves the hard page cap (not an unbounded loop) is what stops it.
    respx.get(url).mock(
        return_value=httpx.Response(200, json=[], headers={"Link": f'<{url}>; rel="next"'})
    )

    with pytest.raises(TransientError, match="pagination exceeded 2 pages"):
        _client().create_or_update_issue(title="Anomaly: ns/pkg", body="details")
