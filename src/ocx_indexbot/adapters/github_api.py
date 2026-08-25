"""GitHub REST + GraphQL client — `ForgePort` implementation (CONTRACTS.md §10).

Plain `httpx` calls only (no SDK, per ADR-4 BD-1's audit-surface driver): REST
for contents/refs/commits/PRs/labels/issues/commit-status, GraphQL only for
`enablePullRequestAutoMerge` (the one mutation with no REST equivalent). The
credential (`token`) is a constructor argument — never read from the
environment inside this module (ADR-4 BD-4) — and never appears in a log
line, `repr()`, or exception message; `token` is excluded from the dataclass
repr (`field(repr=False)`) and every raised message below is built without
it.

`commit_files` uses the Git Data API (blob/tree/commit/ref), never the
per-file Contents API, so a multi-file regenerate (root JSON plus N
image indices) lands as one atomic commit. Branch staleness ("the
branch moved since `base_sha` was read") is detected by GitHub's own
non-fast-forward 422/409 response on the ref update — this adapter does not
pre-check and race a separate read, it lets the write itself be the atomic
conflict check (matches `ports.ForgePort.commit_files`'s documented
contract).

`open_or_update_pull_request` is idempotent per head branch — GitHub allows
only one open PR per branch, so "list PRs for this branch" first, create
only if none exists, and only PATCH title/body when they actually differ
(never a no-op edit, which would spam the PR timeline on every re-run).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import quote

import httpx

from ocx_indexbot.adapters import _http
from ocx_indexbot.adapters._http import (
    as_object,
    as_object_list,
    check_transient,
    paginate,
    raise_for_status,
)
from ocx_indexbot.errors import ForgeError, TransientError
from ocx_indexbot.model import CommitStatusState, PullRequestHeadMatch, PullRequestInfo

if TYPE_CHECKING:
    from collections.abc import Mapping

_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_FORGE = "GitHub"
"""Hard pagination cap (mirrors `adapters/registry_v2.py`'s `tags/list` cap,
CONTRACTS.md §9) — bounds an otherwise-unbounded `Link`-header follow loop
against a pathological or misbehaving response chain."""

_AUTO_MERGE_MUTATION = """
mutation($pullRequestId: ID!, $expectedHeadOid: GitObjectID!) {
  enablePullRequestAutoMerge(
    input: {pullRequestId: $pullRequestId, expectedHeadOid: $expectedHeadOid}
  ) {
    clientMutationId
  }
}
"""
"""`expectedHeadOid` makes the mutation fail if the pull request's head has
moved since the gate judged it. GitHub answers with an `errors[]` payload
rather than a status code, so `enable_auto_merge` reads the message — see
`_HEAD_MOVED_MARKERS`."""

_HEAD_MOVED_MARKERS: Final[tuple[str, ...]] = ("expected head oid", "head branch was modified")
"""GraphQL error text that means "the head moved", lower-cased for matching.

Matching on prose is unpleasant and GitHub could reword it. The failure mode
is bounded on purpose: an unrecognised message still raises, so a reworded
error becomes a loud failure, never a silent arm."""

_ALREADY_MERGEABLE_MARKERS: Final[tuple[str, ...]] = ("clean status", "unstable status")
"""GraphQL error text that means "there is nothing left to wait for".

Arming is only possible while the merge is still *blocked*. When every
required check finishes before the arm call gets there, GitHub refuses the
mutation — `CLEAN`/`UNSTABLE` are already-mergeable states — and the
machine-lane PR would sit open forever waiting for an auto-merge nobody
armed. `enable_auto_merge` performs the identical squash itself in that case.

Lifted verbatim from the shell this replaced (`gh pr merge --auto` surfaces
exactly this GraphQL message), markers included, so the condition the live
lane was tuned against is the condition still matched.
"""

_DISABLE_AUTO_MERGE_MUTATION = """
mutation($pullRequestId: ID!) {
  disablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId}) {
    clientMutationId
  }
}
"""
"""`withdraw_auto_merge`'s mutation — no `expectedHeadOid` guard, because
withdrawing is not a decision about a revision (`ports.ForgePort.
withdraw_auto_merge`)."""


class GraphQLError(ForgeError):
    """A GitHub GraphQL response carried a non-empty `errors[]` payload.

    A `ForgeError`, and for the same reason that class exists: this is the
    forge permanently refusing a write (auto-merge disabled on the
    repository, a token scope too narrow for the mutation), and every caller
    above the adapter layer catches `IndexBotError`. It used to be a bare
    `RuntimeError` on the argument that no ADR mapped a GraphQL failure to an
    exit code — but "unmapped" resolved to "walks through
    `cli/governance_poll.py`'s per-pull-request guard and ends the sweep",
    which is not a decision anyone took.
    """


_FORBIDDEN: Final[int] = 403

_BOT_ACCOUNT_TYPE: Final[str] = "Bot"
"""`user.type` of a GitHub App identity. An ordinary account cannot claim it,
and an App can only comment on a repository it is installed on — which needs
admin there. See `_is_repo_side_author` for why that is the fallback."""


def _is_repo_side_author(comment: Mapping[str, Any], self_user_id: int | None) -> bool:
    """Whether `comment` was written by this side, given the token's own user
    id (`None` when it could not be resolved — see `_self_user_id`).

    With an id, the test is exact: the comment is ours or it is not. It
    replaced an `author_association in {OWNER, MEMBER, COLLABORATOR}` check
    that did not test what its name claimed. `MEMBER` is *organization*
    membership, which on a public org can be self-service and grants no
    permission on this repository at all; `COLLABORATOR` includes the read-
    and triage-only roles. Either could post the public marker and have
    `create_comment` adopt and edit that comment instead of posting the
    governance notice — i.e. choose what the notice says.

    **Residual, when the id is unresolvable.** The fallback admits any
    GitHub App identity, not only this run's. It is not the exact test: a
    second App installed on this repository could hold the marked comment.
    It is chosen because it is the strongest signal an *outside contributor*
    cannot satisfy — `type: "Bot"` is not claimable by an ordinary account,
    and installing an App requires admin on the repository, so the fallback's
    whole population is already repo-side.
    """
    user: Mapping[str, Any] = comment.get("user") or {}
    if self_user_id is None:
        return str(user.get("type", "")) == _BOT_ACCOUNT_TYPE
    return int(user.get("id", 0) or 0) == self_user_id


@dataclass(frozen=True, slots=True)
class GitHubApi:
    """`ForgePort` over plain `httpx` REST + GraphQL calls.

    `owner`/`repo` identify the index repository (e.g. `"ocx-sh"`,
    `"index"`). `token` is redacted from `repr()` and never placed into a
    URL, log line, or exception message anywhere in this module — every
    raised message below is built from method/path/status information only.
    """

    owner: str
    repo: str
    token: str = field(default="", repr=False)
    timeout: float = 30.0
    base_url: str = "https://api.github.com"
    graphql_url: str = "https://api.github.com/graphql"
    _cache: dict[str, Any] = field(
        default_factory=dict[str, Any], repr=False, compare=False, init=False
    )
    """Per-instance memo for the one answer that cannot change inside a run:
    the token's own user id. A governance sweep asks the marker question once
    per open pull request, and `GET /user` would otherwise be re-asked each
    time. `init=False` so no caller can be handed a pre-populated cache, and
    so a `dataclasses.replace` of this adapter starts with an empty one — the
    trap `adapters/gitlab_api.py`'s identical field documents."""

    # ---- ForgePort -----------------------------------------------------------

    def get_file_contents(self, path: str, ref: str) -> bytes | None:
        url = self._repo_url("contents", *path.split("/"))
        with self._client() as client:
            response = client.get(url, params={"ref": ref})
        self._check_transient(response)
        if response.status_code == 404:
            return None
        self._raise(response)
        return base64.b64decode(response.json()["content"])

    def get_ref_sha(self, ref: str) -> str | None:
        with self._client() as client:
            response = client.get(self._repo_url("git", "ref", "heads", ref))
        self._check_transient(response)
        if response.status_code == 404:
            return None
        self._raise(response)
        return str(response.json()["object"]["sha"])

    def commit_files(
        self,
        *,
        branch: str,
        base_sha: str,
        message: str,
        files: Mapping[str, bytes | None],
        base_repo: str | None = None,
    ) -> str:
        # A fork network shares object storage here, so an upstream SHA is
        # already reachable from the fork and needs no help being found.
        del base_repo
        with self._client() as client:
            base_tree_sha = self._get_base_tree_sha(client, base_sha)
            entries = [self._tree_entry(client, path, content) for path, content in files.items()]
            new_tree_sha = self._create_tree(client, base_tree_sha, entries)
            new_commit_sha = self._create_commit(client, message, new_tree_sha, base_sha)
            self._update_branch(client, branch, base_sha, new_commit_sha)
        return new_commit_sha

    def open_or_update_pull_request(
        self, *, branch: str, base: str, title: str, body: str, head_repo: str | None = None
    ) -> int:
        head_owner = head_repo.partition("/")[0] if head_repo else self.owner
        head = f"{head_owner}:{branch}"
        with self._client() as client:
            existing = self._find_open_pull_request(client, head, base)
            if existing is None:
                response = client.post(
                    self._repo_url("pulls"),
                    json={"title": title, "body": body, "head": head, "base": base},
                )
                self._check_transient(response)
                self._raise(response)
                return int(response.json()["number"])

            number = int(existing["number"])
            if existing["title"] != title or existing["body"] != body:
                update_response = client.patch(
                    self._repo_url("pulls", str(number)),
                    json={"title": title, "body": body},
                )
                self._check_transient(update_response)
                self._raise(update_response)
            return number

    def add_labels(self, pr_number: int, labels: list[str]) -> None:
        with self._client() as client:
            response = client.post(
                self._repo_url("issues", str(pr_number), "labels"), json={"labels": labels}
            )
        self._check_transient(response)
        self._raise(response)

    def remove_label(self, pr_number: int, label: str) -> None:
        """404 is the answer for both "no such label on this PR" and "no such
        label in this repository", and neither is a failure of the caller's
        intent — the label is not on the PR either way.

        It is also the answer for a pull request or repository that does not
        exist, which would be a real failure hidden. Not reachable here: every
        caller has already read this pull request through
        `get_pull_request_info` in the same run, and a repository this token
        cannot see fails every other call in the job first."""
        with self._client() as client:
            response = client.delete(self._repo_url("issues", str(pr_number), "labels", label))
        self._check_transient(response)
        if response.status_code == 404:
            return
        self._raise(response)

    def enable_auto_merge(self, pr_number: int, *, head_sha: str) -> None:
        with self._client() as client:
            pr_response = client.get(self._repo_url("pulls", str(pr_number)))
            self._check_transient(pr_response)
            self._raise(pr_response)
            node_id = pr_response.json()["node_id"]

            graphql_response = client.post(
                self.graphql_url,
                json={
                    "query": _AUTO_MERGE_MUTATION,
                    "variables": {"pullRequestId": node_id, "expectedHeadOid": head_sha},
                },
            )
        self._check_transient(graphql_response)
        self._raise(graphql_response)
        errors = graphql_response.json().get("errors")
        if errors:
            message = str(errors[0].get("message", "unknown GraphQL error"))
            lowered = message.lower()
            if any(marker in lowered for marker in _HEAD_MOVED_MARKERS):
                # The head moved between the gate and the arm. Nothing to do:
                # the decision was about a revision that is no longer current,
                # and the next run gates the new one.
                return
            if any(marker in lowered for marker in _ALREADY_MERGEABLE_MARKERS):
                self._squash_merge(pr_number, head_sha=head_sha)
                return
            raise GraphQLError(f"enablePullRequestAutoMerge failed: {message}")

    def _squash_merge(self, pr_number: int, *, head_sha: str) -> None:
        """The already-mergeable fallback: squash the pull request now, pinned
        to the revision the gate judged (see `_ALREADY_MERGEABLE_MARKERS`).

        `sha` is the REST spelling of `gh pr merge --match-head-commit`, and it
        is the same guard `expectedHeadOid` was: a push racing this call answers
        409 rather than merging a revision nothing gated. That 409 returns
        quietly, exactly as a moved head does on the arming path —
        `ports.ForgePort.enable_auto_merge` promises a stale decision is not an
        error, and the event for the new head re-gates it. Nothing else is
        swallowed: a 405 ("not mergeable") still raises, and there is no admin
        override anywhere here, so branch protection binds on this route just
        as it does on the armed one.
        """
        with self._client() as client:
            response = client.put(
                self._repo_url("pulls", str(pr_number), "merge"),
                json={"merge_method": "squash", "sha": head_sha},
            )
        self._check_transient(response)
        if response.status_code == 409:
            return
        self._raise(response)

    def withdraw_auto_merge(self, pr_number: int) -> None:
        with self._client() as client:
            pr_response = client.get(self._repo_url("pulls", str(pr_number)))
            self._check_transient(pr_response)
            self._raise(pr_response)
            payload = pr_response.json()
            if not payload.get("auto_merge"):
                # Nothing armed — the REST pull-request object's own
                # `auto_merge` field is `null` when it is off, so this is a
                # cheap read-only no-op rather than an error from disabling
                # what was never enabled.
                return
            node_id = payload["node_id"]

            graphql_response = client.post(
                self.graphql_url,
                json={
                    "query": _DISABLE_AUTO_MERGE_MUTATION,
                    "variables": {"pullRequestId": node_id},
                },
            )
        self._check_transient(graphql_response)
        self._raise(graphql_response)
        errors = graphql_response.json().get("errors")
        if errors:
            message = str(errors[0].get("message", "unknown GraphQL error"))
            raise GraphQLError(f"disablePullRequestAutoMerge failed: {message}")

    def get_pull_request_info(self, pr_number: int) -> PullRequestInfo:
        """Read the pull request, then its changed files, then **re-read the
        head** and refuse the result if it moved.

        The two facts come from two endpoints and GitHub binds neither to the
        other: `GET /pulls/{n}` carries the head sha, `GET /pulls/{n}/files`
        carries the paths, and a paginated file walk can span many round
        trips. A force-push A -> B -> A across that window returns B's file
        list under A's sha — so `cli/classify_pr.py` classifies content that
        is not there, and `cli/governance_gate.py` arms auto-merge bound to
        `head_sha`, which still resolves. The arm's `expectedHeadOid` guard
        does not help: the sha it checks is exactly the one that came back.

        The re-read closes it by refusing rather than repairing: this is the
        governance lane's one read of what a pull request contains, and a
        wrong answer is worse than no answer. `TransientError` is the right
        shape because the next tick simply re-reads (ADR-4 BD-2, exit 75).
        The window is not eliminated — a push landing after the re-read is
        the same race — but it is narrowed to two adjacent reads with no
        pagination between them, and every classification is now made against
        a head that was still current after the files were listed.
        """
        payload = self._pull_request(pr_number)
        head_sha = str(payload["head"]["sha"])

        files = self._paginate(self._repo_url("pulls", str(pr_number), "files"), {})
        changed_paths = tuple(item["filename"] for item in files)

        if str(self._pull_request(pr_number)["head"]["sha"]) != head_sha:
            raise TransientError(
                f"pull request #{pr_number} was pushed to while its changed files were read"
            )

        return PullRequestInfo(
            number=pr_number,
            base_sha=payload["base"]["sha"],
            head_sha=head_sha,
            changed_paths=changed_paths,
            author_login=payload["user"]["login"],
            author_id=payload["user"]["id"],
            updated_at=payload["updated_at"],
            labels=tuple(label["name"] for label in payload["labels"]),
        )

    def _pull_request(self, pr_number: int) -> dict[str, Any]:
        """`GET /pulls/{n}`, or `KeyError` if there is no such pull request."""
        url = self._repo_url("pulls", str(pr_number))
        with self._client() as client:
            response = client.get(url)
        self._check_transient(response)
        if response.status_code == 404:
            raise KeyError(f"no such pull request: #{pr_number}")
        self._raise(response)
        return as_object(response, forge=_FORGE, url=url)

    def set_commit_status(
        self,
        sha: str,
        *,
        context: str,
        state: CommitStatusState,
        description: str,
        pull_request: int | None = None,
    ) -> None:
        # A fork PR's head is reachable from the base repository
        # (`refs/pull/<n>/head`), so the status API needs no help locating it.
        del pull_request
        with self._client() as client:
            response = client.post(
                self._repo_url("statuses", sha),
                json={"state": state, "context": context, "description": description},
            )
        self._check_transient(response)
        self._raise(response)

    def request_reviewers(self, pr_number: int, logins: list[str]) -> None:
        with self._client() as client:
            response = client.post(
                self._repo_url("pulls", str(pr_number), "requested_reviewers"),
                json={"reviewers": logins},
            )
        self._check_transient(response)
        self._raise(response)

    def create_comment(self, pr_number: int, body: str, *, marker: str) -> None:
        existing = self._find_marked_comment(pr_number, marker)
        if existing is None:
            with self._client() as client:
                response = client.post(
                    self._repo_url("issues", str(pr_number), "comments"), json={"body": body}
                )
            self._check_transient(response)
            self._raise(response)
            return

        comment_id, existing_body = existing
        if existing_body != body:
            with self._client() as client:
                response = client.patch(
                    self._repo_url("issues", "comments", str(comment_id)), json={"body": body}
                )
            self._check_transient(response)
            self._raise(response)

    def list_approvals(self, pr_number: int, *, head_sha: str) -> tuple[int, ...]:
        """Reviews are per-commit here, so a review left on an earlier push is
        simply not an approval of this one.

        `user.id`, never `user.login`: the caller matches these against
        `.github/maintainers.yml`'s `id`, because a login that has been
        renamed and recycled would otherwise carry a former maintainer's veto
        to whoever holds the name now (`ports.ForgePort.list_approvals`).
        """
        reviews = self._paginate(self._repo_url("pulls", str(pr_number), "reviews"), {})
        return tuple(
            sorted(
                {
                    int(review["user"]["id"])
                    for review in reviews
                    if review.get("state") == "APPROVED" and review.get("commit_id") == head_sha
                }
            )
        )

    def list_open_pull_requests(self) -> tuple[int, ...]:
        items = self._paginate(self._repo_url("pulls"), {"state": "open"})
        return tuple(sorted(int(item["number"]) for item in items))

    def resolve_review_thread(self, pr_number: int, *, marker: str) -> None:
        """Nothing to do. An issue comment is not resolvable and blocks no
        merge here; the commit status carries the decision, and branch
        protection is what enforces anything. Present so `core`/`cli` can
        release the gate without asking which forge they are on — see
        `adapters/gitlab_api.py`, where this is the whole gate."""
        del pr_number, marker

    def create_or_update_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> int:
        """Idempotent per exact `title` match among open, non-PR issues.
        `cli/reconcile.py`'s anomaly-report mechanism (promoted onto
        `ports.ForgePort`, fork-PR announce revamp — previously an
        adapter-only capability, CONTRACTS.md §13 item 4's flagged gap)."""
        existing = self._find_open_issue(title)
        if existing is None:
            with self._client() as client:
                response = client.post(
                    self._repo_url("issues"),
                    json={"title": title, "body": body, "labels": labels or []},
                )
            self._check_transient(response)
            self._raise(response)
            return int(response.json()["number"])

        number = int(existing["number"])
        if existing.get("body") != body:
            with self._client() as client:
                response = client.patch(self._repo_url("issues", str(number)), json={"body": body})
            self._check_transient(response)
            self._raise(response)
        return number

    def find_pull_request_by_head_sha(self, head_sha: str) -> PullRequestHeadMatch | None:
        """`GET .../commits/{sha}/pulls` lists every PR *associated with* the
        commit, not just the one currently at its head — filtered below to an
        exact `head.sha` match, first one wins (mirrors `pr-checks-label.yml`'s
        own `jq` filter, `select(.head.sha == $sha)`). A 404 (the SHA names no
        commit this repository knows about at all) is "no match", the same as
        an empty result list.
        """
        url = self._repo_url("commits", head_sha, "pulls")
        with self._client() as client:
            response = client.get(url)
        self._check_transient(response)
        if response.status_code == 404:
            return None
        self._raise(response)
        items = as_object_list(response, forge=_FORGE, url=url)
        for item in items:
            head: dict[str, Any] = item.get("head") or {}
            if head.get("sha") != head_sha:
                continue
            head_repo: dict[str, Any] = head.get("repo") or {}
            full_name = cast("str | None", head_repo.get("full_name"))
            # A null `head.repo` (the fork was deleted) is not evidence the PR
            # is fork-authored — `pr-checks-label.yml` treated a missing name
            # the same as "same repo", i.e. not in scope for FP-8 labeling.
            is_fork = full_name is not None and full_name != f"{self.owner}/{self.repo}"
            return PullRequestHeadMatch(number=int(item["number"]), is_fork=is_fork)
        return None

    def close_pull_request(self, pr_number: int) -> None:
        with self._client() as client:
            response = client.patch(
                self._repo_url("pulls", str(pr_number)), json={"state": "closed"}
            )
        self._check_transient(response)
        self._raise(response)

    # ---- construction / request helpers ----------------------------------------

    def _headers(self) -> dict[str, str]:
        """`Authorization` is omitted entirely when `token` is empty —
        `cli/announce.py`'s `--out` mode reads the index repo's committed
        root anonymously (a public repo's Contents API works unauthenticated,
        just at a lower rate limit); sending `Authorization: Bearer ` with an
        empty token would itself be rejected as a malformed credential."""
        headers = {"Accept": _ACCEPT, "X-GitHub-Api-Version": _API_VERSION}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _client(self) -> httpx.Client:
        return _http.client(headers=self._headers(), timeout=self.timeout, forge=_FORGE)

    def _repo_url(self, *segments: str) -> str:
        quoted = "/".join(quote(segment, safe="") for segment in segments)
        return f"{self.base_url}/repos/{self.owner}/{self.repo}/{quoted}"

    def _check_transient(self, response: httpx.Response) -> None:
        """`adapters/_http.check_transient` under this adapter's label — the
        classification is shared with `adapters/gitlab_api.py` on purpose (see
        that module's docstring)."""
        check_transient(response, forge=_FORGE)

    def _raise(self, response: httpx.Response) -> None:
        """`response.raise_for_status()`, but the failure leaves this layer as
        an `IndexBotError`. A raw `httpx.HTTPStatusError` walks straight
        through `cli/governance_poll.py`'s per-pull-request guard, which is
        how a single 400 ended a whole governance sweep in production."""
        raise_for_status(response, forge=_FORGE)

    def _paginate(self, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
        with self._client() as client:
            return paginate(client, url, params, forge=_FORGE)

    def _find_open_pull_request(
        self, client: httpx.Client, head: str, base: str
    ) -> dict[str, Any] | None:
        """`head` is already the fully-qualified `owner:branch` query value —
        `open_or_update_pull_request` resolves same-repo (`self.owner`) vs.
        cross-repo (`head_repo`'s owner) before calling this."""
        url = self._repo_url("pulls")
        response = client.get(url, params={"head": head, "base": base, "state": "open"})
        self._check_transient(response)
        self._raise(response)
        matches = as_object_list(response, forge=_FORGE, url=url)
        return matches[0] if matches else None

    def _find_open_issue(self, title: str) -> dict[str, Any] | None:
        for item in self._paginate(self._repo_url("issues"), {"state": "open"}):
            if "pull_request" in item:
                continue  # /issues also returns PRs — exclude them
            if item["title"] == title:
                return item
        return None

    def _find_marked_comment(self, pr_number: int, marker: str) -> tuple[int, str] | None:
        """The first existing comment on `pr_number` whose body contains the
        hidden HTML `marker` **and was written by this side** —
        `create_comment`'s idempotency mechanism (G-20: one review-required
        comment per PR across repeated `governance-check` runs, never a fresh
        comment every run).

        The marker is public: it ships in every notice this bot posts, so a
        pull request's own author can copy it into a comment of their own. An
        unfiltered match would make `create_comment` edit *that* comment
        instead of posting the notice, letting the author choose what the
        governance notice says. `_is_repo_side_author` is the filter, and
        carries the argument for what it tests and what it concedes.
        """
        self_user_id = self._self_user_id()
        for item in self._paginate(self._repo_url("issues", str(pr_number), "comments"), {}):
            body = str(item.get("body", ""))
            if marker in body and _is_repo_side_author(item, self_user_id):
                return int(item["id"]), body
        return None

    def _self_user_id(self) -> int | None:
        """The id of the user this token authenticates as, or `None` when that
        cannot be established. Asked once per instance — the answer is fixed
        for the life of a token, and a governance sweep asks it once per open
        pull request.

        `None` has two causes, both ordinary:

        - **No token.** `cli/announce.py`'s `--out` mode reads a public repo
          anonymously; there is nothing to identify.
        - **An installation token.** A workflow's `GITHUB_TOKEN` is scoped to
          the repository, not to a user, and `GET /user` answers it
          `403 Resource not accessible by integration`. This is the common
          case in CI, not an edge one.

        `adapters/gitlab_api.py` asks the same question the same way; GitLab
        has no installation-token equivalent, so its answer is never `None`
        for a real token.
        """
        if "self_user_id" not in self._cache:
            self._cache["self_user_id"] = self._fetch_self_user_id()
        user_id: int | None = self._cache["self_user_id"]
        return user_id

    def _fetch_self_user_id(self) -> int | None:
        if not self.token:
            return None
        with self._client() as client:
            response = client.get(f"{self.base_url}/user")
        self._check_transient(response)
        if response.status_code == _FORBIDDEN:
            # An installation token. `check_transient` has already claimed the
            # 403-with-`Retry-After` secondary-rate-limit case above, so what
            # reaches here is "this credential is not a user" — the answer,
            # not a failure.
            return None
        self._raise(response)
        return int(response.json()["id"])

    def _get_base_tree_sha(self, client: httpx.Client, base_sha: str) -> str:
        response = client.get(self._repo_url("git", "commits", base_sha))
        self._check_transient(response)
        if response.status_code == 404:
            raise TransientError(f"commit_files: base_sha {base_sha!r} not found")
        self._raise(response)
        return str(response.json()["tree"]["sha"])

    def _tree_entry(self, client: httpx.Client, path: str, content: bytes | None) -> dict[str, Any]:
        if content is None:
            return {"path": path, "mode": "100644", "type": "blob", "sha": None}
        response = client.post(
            self._repo_url("git", "blobs"),
            json={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        self._check_transient(response)
        self._raise(response)
        return {"path": path, "mode": "100644", "type": "blob", "sha": response.json()["sha"]}

    def _create_tree(
        self, client: httpx.Client, base_tree_sha: str, entries: list[dict[str, Any]]
    ) -> str:
        response = client.post(
            self._repo_url("git", "trees"), json={"base_tree": base_tree_sha, "tree": entries}
        )
        self._check_transient(response)
        self._raise(response)
        return str(response.json()["sha"])

    def _create_commit(
        self, client: httpx.Client, message: str, tree_sha: str, base_sha: str
    ) -> str:
        response = client.post(
            self._repo_url("git", "commits"),
            json={"message": message, "tree": tree_sha, "parents": [base_sha]},
        )
        self._check_transient(response)
        self._raise(response)
        return str(response.json()["sha"])

    def _update_branch(
        self, client: httpx.Client, branch: str, base_sha: str, new_commit_sha: str
    ) -> None:
        ref_response = client.get(self._repo_url("git", "ref", "heads", branch))
        self._check_transient(ref_response)
        if ref_response.status_code == 404:
            create_response = client.post(
                self._repo_url("git", "refs"),
                json={"ref": f"refs/heads/{branch}", "sha": new_commit_sha},
            )
            self._check_transient(create_response)
            if create_response.status_code == 422:
                raise TransientError(f"branch {branch!r} was created concurrently")
            self._raise(create_response)
            return

        self._raise(ref_response)
        update_response = client.patch(
            self._repo_url("git", "refs", "heads", branch),
            json={"sha": new_commit_sha, "force": False},
        )
        self._check_transient(update_response)
        if update_response.status_code in (409, 422):
            raise TransientError(f"branch {branch!r} moved since base_sha {base_sha!r} was read")
        self._raise(update_response)
