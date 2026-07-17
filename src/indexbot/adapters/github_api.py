"""GitHub REST + GraphQL client — `GitHubPort` implementation (CONTRACTS.md §10).

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
observation objects) lands as one atomic commit. Branch staleness ("the
branch moved since `base_sha` was read") is detected by GitHub's own
non-fast-forward 422/409 response on the ref update — this adapter does not
pre-check and race a separate read, it lets the write itself be the atomic
conflict check (matches `ports.GitHubPort.commit_files`'s documented
contract).

`open_or_update_pull_request` is idempotent per head branch — GitHub allows
only one open PR per branch, so "list PRs for this branch" first, create
only if none exists, and only PATCH title/body when they actually differ
(never a no-op edit, which would spam the PR timeline on every re-run).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from indexbot.errors import TransientError
from indexbot.model import CommitStatusState, PullRequestInfo

if TYPE_CHECKING:
    from collections.abc import Mapping

_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_MAX_PAGES = 100
"""Hard pagination cap (mirrors `adapters/ghcr.py`'s `tags/list` cap,
CONTRACTS.md §9) — bounds an otherwise-unbounded `Link`-header follow loop
against a pathological or misbehaving response chain."""

_AUTO_MERGE_MUTATION = """
mutation($pullRequestId: ID!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId}) {
    clientMutationId
  }
}
"""


class GraphQLError(RuntimeError):
    """A GitHub GraphQL response carried a non-empty `errors[]` payload.

    Deliberately **not** an `IndexBotError` subclass — no ADR maps a GraphQL
    mutation failure (e.g. auto-merge disabled on the repository,
    insufficient token scope) to one of the four exit codes. It propagates
    as an unhandled bug, per `errors.py`'s documented philosophy ("anything
    that is not an `IndexBotError` ... is deliberately left to propagate"),
    until that mapping is a deliberate decision rather than a guess.
    """


@dataclass(frozen=True, slots=True)
class GitHubApi:
    """`GitHubPort` over plain `httpx` REST + GraphQL calls.

    `owner`/`repo` identify the index repository (e.g. `"ocx-sh"`,
    `"index"`). `token` is redacted from `repr()` and never placed into a
    URL, log line, or exception message anywhere in this module — every
    raised message below is built from method/path/status information only.
    """

    owner: str
    repo: str
    token: str = field(repr=False)
    timeout: float = 30.0
    base_url: str = "https://api.github.com"
    graphql_url: str = "https://api.github.com/graphql"

    # ---- GitHubPort -----------------------------------------------------------

    def get_file_contents(self, path: str, ref: str) -> bytes | None:
        url = self._repo_url("contents", *path.split("/"))
        with self._client() as client:
            response = client.get(url, params={"ref": ref})
        self._check_transient(response)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return base64.b64decode(response.json()["content"])

    def get_ref_sha(self, ref: str) -> str | None:
        with self._client() as client:
            response = client.get(self._repo_url("git", "ref", "heads", ref))
        self._check_transient(response)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return str(response.json()["object"]["sha"])

    def commit_files(
        self, *, branch: str, base_sha: str, message: str, files: Mapping[str, bytes | None]
    ) -> str:
        with self._client() as client:
            base_tree_sha = self._get_base_tree_sha(client, base_sha)
            entries = [self._tree_entry(client, path, content) for path, content in files.items()]
            new_tree_sha = self._create_tree(client, base_tree_sha, entries)
            new_commit_sha = self._create_commit(client, message, new_tree_sha, base_sha)
            self._update_branch(client, branch, base_sha, new_commit_sha)
        return new_commit_sha

    def open_or_update_pull_request(self, *, branch: str, base: str, title: str, body: str) -> int:
        with self._client() as client:
            existing = self._find_open_pull_request(client, branch, base)
            if existing is None:
                response = client.post(
                    self._repo_url("pulls"),
                    json={"title": title, "body": body, "head": branch, "base": base},
                )
                self._check_transient(response)
                response.raise_for_status()
                return int(response.json()["number"])

            number = int(existing["number"])
            if existing["title"] != title or existing["body"] != body:
                update_response = client.patch(
                    self._repo_url("pulls", str(number)),
                    json={"title": title, "body": body},
                )
                self._check_transient(update_response)
                update_response.raise_for_status()
            return number

    def add_labels(self, pr_number: int, labels: list[str]) -> None:
        with self._client() as client:
            response = client.post(
                self._repo_url("issues", str(pr_number), "labels"), json={"labels": labels}
            )
        self._check_transient(response)
        response.raise_for_status()

    def enable_auto_merge(self, pr_number: int) -> None:
        with self._client() as client:
            pr_response = client.get(self._repo_url("pulls", str(pr_number)))
            self._check_transient(pr_response)
            pr_response.raise_for_status()
            node_id = pr_response.json()["node_id"]

            graphql_response = client.post(
                self.graphql_url,
                json={"query": _AUTO_MERGE_MUTATION, "variables": {"pullRequestId": node_id}},
            )
        self._check_transient(graphql_response)
        graphql_response.raise_for_status()
        errors = graphql_response.json().get("errors")
        if errors:
            message = errors[0].get("message", "unknown GraphQL error")
            raise GraphQLError(f"enablePullRequestAutoMerge failed: {message}")

    def get_pull_request_info(self, pr_number: int) -> PullRequestInfo:
        with self._client() as client:
            response = client.get(self._repo_url("pulls", str(pr_number)))
        self._check_transient(response)
        if response.status_code == 404:
            raise KeyError(f"no such pull request: #{pr_number}")
        response.raise_for_status()
        payload = response.json()

        files = self._paginate(self._repo_url("pulls", str(pr_number), "files"), {})
        changed_paths = tuple(item["filename"] for item in files)

        return PullRequestInfo(
            number=pr_number,
            base_sha=payload["base"]["sha"],
            head_sha=payload["head"]["sha"],
            changed_paths=changed_paths,
        )

    def set_commit_status(
        self, sha: str, *, context: str, state: CommitStatusState, description: str
    ) -> None:
        with self._client() as client:
            response = client.post(
                self._repo_url("statuses", sha),
                json={"state": state, "context": context, "description": description},
            )
        self._check_transient(response)
        response.raise_for_status()

    # ---- extra capability, not on ports.GitHubPort -----------------------------

    def create_or_update_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> int:
        """Idempotent per exact `title` match among open, non-PR issues.

        Not part of `ports.GitHubPort` as of this work package — CONTRACTS.md
        §13 item 4 flags that no `create_or_update_issue`-shaped method
        exists on the frozen Protocol yet. This adapter adds the capability
        `cli/reconcile.py` (a different, later work package) needs for its
        anomaly-report requirement, without editing `ports.py`/
        `tests/fakes/__init__.py` out from under parallel builders — both
        are out of this work package's assigned scope. See this work
        package's `open_questions`: `ports.GitHubPort` should gain a
        matching method (and `FakeGitHub` a matching fake) once an owning
        work package is assigned.
        """
        existing = self._find_open_issue(title)
        if existing is None:
            with self._client() as client:
                response = client.post(
                    self._repo_url("issues"),
                    json={"title": title, "body": body, "labels": labels or []},
                )
            self._check_transient(response)
            response.raise_for_status()
            return int(response.json()["number"])

        number = int(existing["number"])
        if existing.get("body") != body:
            with self._client() as client:
                response = client.patch(self._repo_url("issues", str(number)), json={"body": body})
            self._check_transient(response)
            response.raise_for_status()
        return number

    # ---- construction / request helpers ----------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(headers=self._headers(), timeout=self.timeout)

    def _repo_url(self, *segments: str) -> str:
        quoted = "/".join(quote(segment, safe="") for segment in segments)
        return f"{self.base_url}/repos/{self.owner}/{self.repo}/{quoted}"

    def _check_transient(self, response: httpx.Response) -> None:
        """Raise `TransientError` for response classes this adapter treats as
        give-up-and-retry-later, uniformly across every call site: auth
        rejection (401 — this adapter's token is fixed for its lifetime, no
        mid-run refresh like `adapters/ghcr.py`'s anonymous pull token, so a
        401 here is not backoff-retryable within the same process — it maps
        straight to the exit-75 "retry later" contract, BD-2); rate limiting
        (429 always; 403 only when `Retry-After` is present, so a bare
        permission-denied 403 is left to `raise_for_status()` as a genuine
        config bug rather than silently retried); and any 5xx.
        """
        status = response.status_code
        if status == 401:
            raise TransientError("GitHub API rejected the request: invalid or expired token")
        if status == 429 or (status == 403 and "Retry-After" in response.headers):
            retry_after = response.headers.get("Retry-After")
            suffix = f" (retry after {retry_after}s)" if retry_after else ""
            raise TransientError(f"GitHub API rate limit exceeded{suffix}")
        if status >= 500:
            raise TransientError(f"GitHub API server error: {status}")

    def _paginate(self, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
        """Follow `Link: rel="next"` up to `_MAX_PAGES`, `per_page=100`."""
        items: list[dict[str, Any]] = []
        current_url = url
        current_params: dict[str, str] | None = {**params, "per_page": "100"}
        with self._client() as client:
            for _ in range(_MAX_PAGES):
                response = client.get(current_url, params=current_params)
                self._check_transient(response)
                response.raise_for_status()
                items.extend(response.json())
                next_link = response.links.get("next", {}).get("url")
                if next_link is None:
                    return items
                current_url = next_link
                current_params = None
        raise TransientError(f"GitHub API pagination exceeded {_MAX_PAGES} pages: {url}")

    def _find_open_pull_request(
        self, client: httpx.Client, branch: str, base: str
    ) -> dict[str, Any] | None:
        response = client.get(
            self._repo_url("pulls"),
            params={"head": f"{self.owner}:{branch}", "base": base, "state": "open"},
        )
        self._check_transient(response)
        response.raise_for_status()
        matches: list[dict[str, Any]] = response.json()
        return matches[0] if matches else None

    def _find_open_issue(self, title: str) -> dict[str, Any] | None:
        for item in self._paginate(self._repo_url("issues"), {"state": "open"}):
            if "pull_request" in item:
                continue  # /issues also returns PRs — exclude them
            if item["title"] == title:
                return item
        return None

    def _get_base_tree_sha(self, client: httpx.Client, base_sha: str) -> str:
        response = client.get(self._repo_url("git", "commits", base_sha))
        self._check_transient(response)
        if response.status_code == 404:
            raise TransientError(f"commit_files: base_sha {base_sha!r} not found")
        response.raise_for_status()
        return str(response.json()["tree"]["sha"])

    def _tree_entry(self, client: httpx.Client, path: str, content: bytes | None) -> dict[str, Any]:
        if content is None:
            return {"path": path, "mode": "100644", "type": "blob", "sha": None}
        response = client.post(
            self._repo_url("git", "blobs"),
            json={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        self._check_transient(response)
        response.raise_for_status()
        return {"path": path, "mode": "100644", "type": "blob", "sha": response.json()["sha"]}

    def _create_tree(
        self, client: httpx.Client, base_tree_sha: str, entries: list[dict[str, Any]]
    ) -> str:
        response = client.post(
            self._repo_url("git", "trees"), json={"base_tree": base_tree_sha, "tree": entries}
        )
        self._check_transient(response)
        response.raise_for_status()
        return str(response.json()["sha"])

    def _create_commit(
        self, client: httpx.Client, message: str, tree_sha: str, base_sha: str
    ) -> str:
        response = client.post(
            self._repo_url("git", "commits"),
            json={"message": message, "tree": tree_sha, "parents": [base_sha]},
        )
        self._check_transient(response)
        response.raise_for_status()
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
            create_response.raise_for_status()
            return

        ref_response.raise_for_status()
        update_response = client.patch(
            self._repo_url("git", "refs", "heads", branch),
            json={"sha": new_commit_sha, "force": False},
        )
        self._check_transient(update_response)
        if update_response.status_code in (409, 422):
            raise TransientError(f"branch {branch!r} moved since base_sha {base_sha!r} was read")
        update_response.raise_for_status()
