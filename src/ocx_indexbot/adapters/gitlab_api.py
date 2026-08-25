"""GitLab REST v4 client — the second `ForgePort` implementation.

Plain `httpx`, no SDK, for the same reason `adapters/github_api.py` uses
none: ADR-4 BD-1 keeps `httpx` the only runtime dependency so the privileged
governance job's audit surface stays readable. Authentication is the
`PRIVATE-TOKEN` header (personal, group, or project access token) — never the
`CI_JOB_TOKEN`, which cannot write MRs, labels, or notes.

A **project** here is whatever identifies it to the API: either the numeric
id (`CI_PROJECT_ID`, what CI hands you) or the URL-encoded path
(`group/subgroup/index`). Both are accepted verbatim and quoted once.

## Where GitLab is genuinely different, not just spelled differently

**Commits are not optimistically locked.** GitHub's ref update rejects a
non-fast-forward, which is what makes `commit_files`'s documented staleness
contract free there. GitLab's `POST /repository/commits` commits on top of
whatever the branch tip *is* and never consults `base_sha`, so this adapter
reads the tip and refuses the write itself when it has moved. That leaves a
read-then-write window GitHub does not have; it is narrow, and the announce
lane it serves is one publisher pushing to their own fork branch. Recorded
here rather than hidden, because "never silently rebased onto a fresh base"
is a contract, not an implementation detail.

**There is no upsert action.** A GitLab commit action is `create` (fails if
the path exists) or `update` (fails if it does not), so this adapter probes
each path at `base_sha` before choosing. GitHub's tree API needs no such
probe. The cost is one `HEAD` per changed file, on a call that already writes
a whole commit.

**`reviewer_ids` replaces the reviewer set** where GitHub's endpoint adds to
it. `cli/governance_check.py` recomputes the full list every run, so replace
is the same outcome and is additionally idempotent — but an *empty* list
would clear the reviewers a human had set, so an empty call is a no-op here.

**MRs are created from the source project.** GitHub opens a cross-fork PR
against the target repo with `head=owner:branch`; GitLab opens it against the
*fork* with `target_project_id` pointing back. `head_repo` therefore carries
a full project path (`user/e2e-indexbot-fork`), not a bare username. The
returned `iid` belongs to the target project either way, so every later call
(`add_labels`, `create_comment`, …) addresses the index project as usual.
"""

from __future__ import annotations

import base64
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import quote

import httpx

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

_FORGE = "GitLab"

GITLAB_API_URL: Final[str] = "https://gitlab.com/api/v4"
"""gitlab.com's API root. A self-hosted instance passes its own — in CI that
is `$CI_API_V4_URL`, which the runner sets on every job."""


def _day_before(timestamp: str) -> str:
    """The `YYYY-MM-DD` one day before an ISO-8601 `timestamp`.

    GitLab's events API takes `after` as a date, exclusive, in the project's
    own timezone. One day of slack absorbs both that exclusivity and any
    timezone offset; nothing about correctness rests on it (see
    `_approver_ids_since`).
    """
    day = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
    return (day - timedelta(days=1)).isoformat()


_BAD_REQUEST: Final[int] = 400

_HEAD_MOVED_STATUSES: Final[frozenset[int]] = frozenset({406, 409})
"""How GitLab answers a merge call whose `sha` no longer names the source
branch head. 409 is the documented conflict; 406 is what the endpoint returns
in practice for a not-mergeable state, and a moved head is one. Both mean the
same thing to the caller: the decision was about a revision that is no longer
current."""

_TRANSITION_FROM_RE: Final[re.Pattern[str]] = re.compile(r"\bfrom :([a-z_]+)\b")
"""The current state named inside GitLab's illegal-transition refusal.

`Cannot transition status via :enqueue from :pending` — the verb is GitLab's
internal event name and varies by target state, but the `from :<state>` half
is always the state vocabulary the status API itself reports.
"""

_STATUS_STATE: Final[dict[CommitStatusState, str]] = {
    "success": "success",
    "pending": "pending",
    "failure": "failed",
    "error": "failed",
}
"""`CommitStatusState` -> GitLab's own vocabulary.

GitLab has no `error`, so both failing states fold onto `failed`. The fold is
one-way and lossless in the direction that matters: `core/` only ever asks
"did the gate pass", and both inputs answer no.
"""


@dataclass(frozen=True, slots=True)
class GitLabApi:
    """`ForgePort` over GitLab REST v4.

    `token` is excluded from `repr()` and never placed into a URL, log line,
    or exception message — every message raised below is built from
    method/path/status information only, exactly as in
    `adapters/github_api.py`.
    """

    project: str
    token: str = field(default="", repr=False)
    timeout: float = 30.0
    base_url: str = GITLAB_API_URL
    _cache: dict[str, Any] = field(
        default_factory=dict[str, Any], repr=False, compare=False, init=False
    )
    """Per-instance memo for the one answer that cannot change inside a run:
    the token's own user id. It gates a per-merge-request decision, so a poll
    sweep would otherwise re-ask it once per open merge request.

    `init=False` is load-bearing, not tidiness. `commit_files` calls
    `dataclasses.replace(self, project=...)` to build a *differently scoped*
    adapter, and `replace` copies every `init=True` field by reference — so
    the new instance would share this dict, project-scoped entries included.
    Nothing is wrongly scoped today (the only key is token identity, which is
    project-independent), and that is the whole trap: the next per-project
    answer cached here would silently be answered for the wrong project.
    With `init=False`, `replace` cannot pass it and the copy starts empty."""

    # ---- ForgePort -------------------------------------------------------------

    def get_file_contents(self, path: str, ref: str) -> bytes | None:
        """The `/raw` variant, so the bytes arrive as bytes — the JSON variant
        would base64-encode them only for this method to decode again."""
        with self._client() as client:
            response = client.get(
                self._project_url("repository", "files", path, "raw"), params={"ref": ref}
            )
        self._check(response)
        if response.status_code == 404:
            return None
        self._raise(response)
        return response.content

    def get_ref_sha(self, ref: str) -> str | None:
        with self._client() as client:
            response = client.get(self._project_url("repository", "branches", ref))
        self._check(response)
        if response.status_code == 404:
            return None
        self._raise(response)
        return str(response.json()["commit"]["id"])

    def commit_files(
        self,
        *,
        branch: str,
        base_sha: str,
        message: str,
        files: Mapping[str, bytes | None],
        base_repo: str | None = None,
    ) -> str:
        """One atomic commit, and — when `base_repo` is given — a branch cut
        from another project's history.

        A GitLab fork does **not** share object storage with its upstream.
        Measured 2026-08-25: the fork answers `404 Commit Not Found` for the
        upstream tip and refuses to create a ref at it. `start_project` is the
        way across, and it is why the announce lane works here at all.
        """
        start_project = base_repo or self.project
        tip = self.get_ref_sha(branch)
        if tip is not None and tip != base_sha:
            raise TransientError(f"branch {branch!r} moved since base_sha {base_sha!r} was read")

        # Probed at the START point, which is where the commit will be applied
        # — not at this project's own default branch, which may not contain
        # these paths at all.
        probe_at = self if tip is not None else replace(self, project=start_project)
        payload: dict[str, Any] = {
            "branch": branch,
            "commit_message": message,
            "actions": [
                probe_at.build_action(path, content, base_sha) for path, content in files.items()
            ],
        }
        if tip is None:
            # Only legal when the branch does not exist yet: GitLab rejects a
            # start point for a branch it would have to fast-forward.
            payload["start_sha"] = base_sha
            if base_repo is not None:
                payload["start_project"] = base_repo

        with self._client() as client:
            response = client.post(self._project_url("repository", "commits"), json=payload)
        self._check(response)
        if response.status_code == 400:
            # GitLab answers 400 for a lost race AND for several permanent
            # mistakes (a `create` on a path that exists, an unreachable start
            # point). Its own message is the only thing that tells them apart,
            # so it is carried through rather than replaced by a guess.
            detail = response.json().get("message", response.text)
            raise TransientError(
                f"commit to branch {branch!r} from base_sha {base_sha!r} was refused: {detail}"
            )
        self._raise(response)
        return str(response.json()["id"])

    def open_or_update_pull_request(
        self, *, branch: str, base: str, title: str, body: str, head_repo: str | None = None
    ) -> int:
        source_project = head_repo or self.project
        source_project_id = self._project_id(source_project) if head_repo else None

        existing = self._find_open_merge_request(branch, base, source_project_id)
        if existing is not None:
            number = int(existing["iid"])
            if existing["title"] != title or existing.get("description") != body:
                self._put(
                    self._project_url("merge_requests", str(number)),
                    {"title": title, "description": body},
                )
            return number

        payload: dict[str, Any] = {
            "source_branch": branch,
            "target_branch": base,
            "title": title,
            "description": body,
        }
        if head_repo:
            payload["target_project_id"] = self._project_id(self.project)
        with self._client() as client:
            response = client.post(
                self._url("projects", source_project, "merge_requests"), json=payload
            )
        self._check(response)
        self._raise(response)
        return int(response.json()["iid"])

    def add_labels(self, pr_number: int, labels: list[str]) -> None:
        """`add_labels` merges into the existing set; assigning `labels`
        would replace it and drop whatever a human had put there."""
        self._put(
            self._project_url("merge_requests", str(pr_number)),
            {"add_labels": ",".join(labels)},
        )

    def remove_label(self, pr_number: int, label: str) -> None:
        """`remove_labels` is the mirror of `add_labels`: a delta, not an
        assignment, so a label that is not there is simply not removed and
        GitLab answers 200 either way."""
        self._put(
            self._project_url("merge_requests", str(pr_number)),
            {"remove_labels": label},
        )

    def enable_auto_merge(self, pr_number: int, *, head_sha: str) -> None:
        """GitLab's auto-merge is a flag on the merge call itself — no GraphQL
        counterpart to GitHub's `enablePullRequestAutoMerge`.

        `sha` is GitLab's own optimistic-concurrency guard: the merge is
        refused unless it still names the source branch's head. That is
        exactly the binding the gate needs, since the classification was made
        against `head_sha` one round-trip earlier. A refusal means the head
        moved, which is not a failure — the next sweep gates the new revision
        — so it returns quietly instead of ending the run.

        **Refused is not the same as armed, and the caller cannot tell.**
        406 is GitLab's *generic* not-mergeable answer: a moved head, yes, but
        also a conflict, a draft, or unresolved discussions — states that do
        not clear on the next tick. Swallowing it is still right (none of them
        is this call's failure), but swallowing it *silently* makes
        `cli/governance_poll.py` print `refresh -> success` for a merge request
        nothing armed, which is the opposite of what that per-merge-request
        line exists to tell a human. So the refusal goes to stderr, beside
        those lines, carrying GitLab's own explanation — which is the only
        thing that distinguishes the four causes.
        """
        with self._client() as client:
            response = client.put(
                self._project_url("merge_requests", str(pr_number), "merge"),
                json={"merge_when_pipeline_succeeds": True, "sha": head_sha},
            )
        self._check(response)
        if response.status_code in _HEAD_MOVED_STATUSES:
            detail = str(response.json().get("message", response.text))[:200]
            print(
                f"gitlab: auto-merge not armed for !{pr_number} ({response.status_code}): {detail}",
                file=sys.stderr,
            )
            return
        self._raise(response)

    def withdraw_auto_merge(self, pr_number: int) -> None:
        """Read `merge_when_pipeline_succeeds` first; only call the cancel
        endpoint when it is set. GitLab's cancel endpoint answers 406 on a
        merge request it was never armed on — the ordinary human-lane case —
        so the read-then-act shape is what makes this a no-op there rather
        than a raised error every poll tick.
        """
        with self._client() as client:
            mr_response = client.get(self._project_url("merge_requests", str(pr_number)))
            self._check(mr_response)
            self._raise(mr_response)
            if not mr_response.json().get("merge_when_pipeline_succeeds"):
                return
            response = client.post(
                self._project_url(
                    "merge_requests", str(pr_number), "cancel_merge_when_pipeline_succeeds"
                )
            )
        self._check(response)
        self._raise(response)

    def get_pull_request_info(self, pr_number: int) -> PullRequestInfo:
        """Read the merge request, then its diffs, then **re-read the head**
        and refuse the result if it moved.

        The same two-unbound-reads race `adapters/github_api.py`'s counterpart
        documents at length, in GitLab's spelling: `diff_refs.head_sha` comes
        from the merge-request entity and the paths from a separate paginated
        `/diffs` walk, with nothing pinning the second to the first. A push
        landing between them yields a classification of content that is not at
        the sha the gate then arms against. Refuse and let the next tick
        re-read.
        """
        payload = self._merge_request(pr_number)
        diff_refs = self._diff_refs(payload, pr_number)

        diffs = self._paginate(self._project_url("merge_requests", str(pr_number), "diffs"), {})
        # `new_path` covers additions, modifications and renames; for a
        # deletion GitLab repeats the old path there, which is also what
        # GitHub's `filename` reports.
        changed_paths = tuple(item["new_path"] for item in diffs)

        fresh = self._diff_refs(self._merge_request(pr_number), pr_number)
        if fresh["head_sha"] != diff_refs["head_sha"]:
            raise TransientError(
                f"merge request !{pr_number} was pushed to while its diffs were read"
            )

        return PullRequestInfo(
            number=pr_number,
            base_sha=diff_refs["base_sha"],
            head_sha=diff_refs["head_sha"],
            changed_paths=changed_paths,
            author_login=payload["author"]["username"],
            author_id=payload["author"]["id"],
            updated_at=payload["updated_at"],
            labels=tuple(payload["labels"]),
        )

    def _merge_request(self, pr_number: int) -> dict[str, Any]:
        """`GET /merge_requests/{iid}`, or `KeyError` if there is no such one."""
        url = self._project_url("merge_requests", str(pr_number))
        with self._client() as client:
            response = client.get(url)
        self._check(response)
        if response.status_code == 404:
            raise KeyError(f"no such merge request: !{pr_number}")
        self._raise(response)
        return as_object(response, forge=_FORGE, url=url)

    def _diff_refs(self, payload: Mapping[str, Any], pr_number: int) -> dict[str, Any]:
        """`payload`'s `diff_refs`, or `TransientError`.

        A just-opened merge request reports `diff_refs: null` until GitLab has
        computed the diff. Retryable, never a classification made against SHAs
        this adapter invented. `diff_refs` is a field nested inside an already
        object-validated payload (`_merge_request` went through `as_object`),
        not a fresh response body, so this checks its own shape directly
        rather than routing back through `as_object`.
        """
        diff_refs = payload.get("diff_refs")
        if not diff_refs:
            raise TransientError(f"merge request !{pr_number} has no diff refs yet")
        if not isinstance(diff_refs, dict):
            raise ForgeError(
                f"{_FORGE} API returned a non-object diff_refs for merge request !{pr_number}"
            )
        return cast("dict[str, Any]", diff_refs)

    def set_commit_status(
        self,
        sha: str,
        *,
        context: str,
        state: CommitStatusState,
        description: str,
        pull_request: int | None = None,
    ) -> None:
        """A report, never the gate — see `ports.ForgePort.set_commit_status`.

        `ref` is not optional in practice. A fork merge request's head commit
        reaches this project only through `refs/merge-requests/<iid>/head`,
        and GitLab's status API 404s on a commit it cannot place on a ref.
        """
        payload: dict[str, Any] = {
            "state": _STATUS_STATE[state],
            "name": context,
            "description": description,
        }
        if pull_request is not None:
            payload["ref"] = f"refs/merge-requests/{pull_request}/head"
        with self._client() as client:
            response = client.post(self._project_url("statuses", sha), json=payload)
        self._check(response)
        if response.status_code == _BAD_REQUEST and self._is_same_state_repost(
            response, sha, context=context, state=state
        ):
            return
        self._raise(response)

    def _is_same_state_repost(
        self,
        response: httpx.Response,
        sha: str,
        *,
        context: str,
        state: CommitStatusState,
    ) -> bool:
        """Whether this 400 means "the context already reports exactly that".

        Two independent readings, because the first one is free and the second
        one has already been observed to disagree with reality in production.

        **The refusal itself.** GitLab names the current state in the body:

            {"message": "Cannot transition status via :enqueue from :pending
                         (Reason(s): Status cannot transition via
                         \"enqueue\")"}

        `from :<state>` is GitLab's own answer to "what does this context hold
        right now", from the same request that refused, about the same
        (project, sha, ref) tuple the POST addressed. Nothing can scope it
        differently and no second call can race it. When it equals the state
        being posted, the write was a no-op and the caller wanted a no-op.

        **The listing**, `_already_reports`, stays as the fallback for a
        message this does not parse — a GitLab release rewording it, or a
        self-hosted instance translating it. It is second rather than first
        because it reads a *different* endpoint: `/repository/commits/<sha>/
        statuses` is not scoped by the `ref` the POST carried, and for a fork
        merge request whose head reaches this project only through
        `refs/merge-requests/<iid>/head` the two do not always answer the same
        question. That mismatch is what ended a real governance sweep
        (`indexbot-governance-poll`, 2026-08-25): the POST was refused with
        `from :pending` while the listing did not report `pending`, so the
        adapter re-raised a 400 that meant "already correct".

        Fails closed either way: an unparsed message and a listing that does
        not match still reach `_raise`.
        """
        wanted = _STATUS_STATE[state]
        match = _TRANSITION_FROM_RE.search(response.text)
        if match is not None:
            return match.group(1) == wanted
        return self._already_reports(sha, context=context, state=state)

    def _already_reports(self, sha: str, *, context: str, state: CommitStatusState) -> bool:
        """Whether `sha` already carries `context` in `state`.

        A GitLab commit status is a state machine, and re-posting the state it
        is already in is an illegal transition rather than a no-op. Measured
        on gitlab.com:

            POST /projects/<id>/statuses/<sha> {"state": "pending", ...}  201
            POST   the same payload again                                400
              {"message": "Cannot transition status via :enqueue from
                           :pending (Reason(s): Status cannot transition
                           via \"enqueue\")"}

        `pending` is the human lane's steady state, so without this the second
        poll tick of any merge request awaiting review raises — and did, in
        production, ending the whole sweep.

        Only reached on a 400, so the extra read costs nothing on the ordinary
        path. Any other 400 still falls through and raises.

        Paginated, via the adapter's own walker. GitLab's default page is 20
        statuses and the endpoint lists *every* status on the commit, not only
        this context's — a commit carrying a handful of pipeline jobs pushes
        `governance/review-required` off page 1 easily. A single-page read
        fails closed (it re-raises the 400 rather than swallowing it), which is
        the safe direction but is also exactly the production failure this
        method was written to stop: the sweep ends on a merge request sitting
        in its steady `pending` state.
        """
        statuses = self._paginate(self._project_url("repository", "commits", sha, "statuses"), {})
        wanted = _STATUS_STATE[state]
        return any(
            item.get("name") == context and item.get("status") == wanted for item in statuses
        )

    def request_reviewers(self, pr_number: int, logins: list[str]) -> None:
        if not logins:
            return
        reviewer_ids = [self._user_id(login) for login in logins]
        self._put(
            self._project_url("merge_requests", str(pr_number)),
            {"reviewer_ids": reviewer_ids},
        )

    def create_comment(self, pr_number: int, body: str, *, marker: str) -> None:
        """A *discussion*, not a note — and that is the merge gate.

        A plain note is not resolvable and blocks nothing. A discussion left
        unresolved makes `detailed_merge_status` report
        `discussions_not_resolved` under the project's "All threads must be
        resolved" setting: Free-tier, controlled by the parent project, and —
        unlike an external commit status — effective on a fork merge request,
        whose head pipeline is the fork's own. `resolve_review_thread` is what
        releases it.
        """
        existing = self._find_marked_discussion(pr_number, marker)
        if existing is None:
            with self._client() as client:
                response = client.post(
                    self._project_url("merge_requests", str(pr_number), "discussions"),
                    json={"body": body},
                )
            self._check(response)
            self._raise(response)
            return

        discussion_id, note_id, existing_body = existing
        base = self._project_url("merge_requests", str(pr_number), "discussions")
        if existing_body != body:
            self._put(f"{base}/{discussion_id}/notes/{note_id}", {"body": body})
        # Re-open it: a maintainer who resolved the thread and then pushed a
        # change that still needs review must not find the gate already
        # released.
        self._put(f"{base}/{discussion_id}", {"resolved": False})

    def resolve_review_thread(self, pr_number: int, *, marker: str) -> None:
        existing = self._find_marked_discussion(pr_number, marker)
        if existing is None:
            return
        discussion_id, _, _ = existing
        self._put(
            self._project_url("merge_requests", str(pr_number), "discussions", discussion_id),
            {"resolved": True},
        )

    def list_approvals(self, pr_number: int, *, head_sha: str) -> tuple[int, ...]:
        """The approvals that were granted to **this** revision, by user id.

        GitLab's approval objects carry no commit, so `head_sha` cannot be
        matched against them the way `adapters/github_api.py` matches a
        review's `commit_id`. The documented staleness control is a project
        setting — "Remove all approvals when commits are added"
        (`reset_approvals_on_push`) — and it is **Premium**: measured on
        gitlab.com Free, `POST …/approvals` accepts the write and the value
        stays `false`. Trusting the setting therefore fails open on exactly
        the tier this lane is designed for.

        Two server-generated timestamps answer it instead, both readable on
        Free and neither forgeable by the author (a committer date is; these
        are not):

        - `…/merge_requests/<iid>/versions[0].created_at` — when the source
          branch last changed. A new version is what a push produces.
        - `…/events?action=approved&target_type=merge_request` — when each
          approval was granted, by whom.

        An approval counts only if it is newer than the newest version. That
        closes approval replay: approve revision A, push unreviewed revision
        B, and the auto-merge lane acts on A's approval.

        Approving is a Free-tier feature; only *requiring* approvals is not,
        which is why the gate is a commit status and this is only its release.

        Both reads are joined on `user.id`, never on `username`: the caller
        matches these against `.github/maintainers.yml`'s `github_id`, and a
        GitLab username is renameable and recyclable exactly as a GitHub login
        is (`ports.ForgePort.list_approvals`).
        """
        approved_by = self._approver_ids(pr_number)
        if not approved_by:
            return ()
        changed_at = self._source_branch_changed_at(pr_number, head_sha=head_sha)
        if changed_at is None:
            return ()
        return tuple(sorted(approved_by & self._approver_ids_since(pr_number, changed_at)))

    def _approver_ids(self, pr_number: int) -> set[int]:
        with self._client() as client:
            response = client.get(self._project_url("merge_requests", str(pr_number), "approvals"))
        self._check(response)
        self._raise(response)
        approved_by: list[dict[str, Any]] = response.json().get("approved_by") or []
        return {int(entry["user"]["id"]) for entry in approved_by}

    def _source_branch_changed_at(self, pr_number: int, *, head_sha: str) -> str | None:
        """When the source branch last moved, or `None` if that cannot be
        established for `head_sha`.

        `None` is the fail-closed answer and has two causes: a merge request
        with no diff versions at all, and a newest version whose head is not
        the revision being gated — which means the branch moved between the
        classification and this read, so every approval is about to be
        re-judged on the next tick anyway.
        """
        versions = self._paginate(
            self._project_url("merge_requests", str(pr_number), "versions"), {}
        )
        if not versions:
            return None
        newest = max(versions, key=lambda version: str(version.get("created_at", "")))
        if str(newest.get("head_commit_sha", "")) != head_sha:
            return None
        return str(newest["created_at"])

    def _approver_ids_since(self, pr_number: int, changed_at: str) -> set[int]:
        """Which user ids approved this merge request after `changed_at`.

        The `after` bound is a *date*, so it is deliberately one day wider
        than the comparison itself — it exists to keep the page count down on
        a busy project, not to decide freshness. The `>` below decides that.

        An event with no author id contributes `0`, which is not a GitLab user
        id and therefore intersects with nothing — the same total, branch-free
        shape `target_iid` above already uses.
        """
        events = self._paginate(
            self._url("projects", self.project, "events"),
            {
                "action": "approved",
                "target_type": "merge_request",
                "after": _day_before(changed_at),
            },
        )
        ids: set[int] = set()
        for event in events:
            author: Mapping[str, Any] = event.get("author") or {}
            if int(event.get("target_iid", 0) or 0) == pr_number and (
                str(event.get("created_at", "")) > changed_at
            ):
                ids.add(int(author.get("id", 0) or 0))
        return ids

    def list_open_pull_requests(self) -> tuple[int, ...]:
        items = self._paginate(self._project_url("merge_requests"), {"state": "opened"})
        return tuple(sorted(int(item["iid"]) for item in items))

    def create_or_update_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> int:
        issues_url = self._project_url("issues")
        for item in self._paginate(issues_url, {"state": "opened"}):
            if item["title"] != title:
                continue
            number = int(item["iid"])
            if item.get("description") != body:
                self._put(self._project_url("issues", str(number)), {"description": body})
            return number

        with self._client() as client:
            response = client.post(
                issues_url,
                json={"title": title, "description": body, "labels": ",".join(labels or [])},
            )
        self._check(response)
        self._raise(response)
        return int(response.json()["iid"])

    def find_pull_request_by_head_sha(self, head_sha: str) -> PullRequestHeadMatch | None:
        """`GET .../commits/{sha}/merge_requests` lists every MR *associated
        with* the commit, not just the one currently at its head — filtered
        below to an exact match on the MR entity's own `sha` field (the head
        commit of the source branch), first one wins, mirroring
        `adapters/github_api.py`'s identical filter over GitHub's `head.sha`.
        A 404 (the SHA names no commit this project knows about) is "no
        match", the same as an empty result list.
        """
        url = self._project_url("repository", "commits", head_sha, "merge_requests")
        with self._client() as client:
            response = client.get(url)
        self._check(response)
        if response.status_code == 404:
            return None
        self._raise(response)
        for item in as_object_list(response, forge=_FORGE, url=url):
            if item.get("sha") != head_sha:
                continue
            is_fork = item.get("source_project_id") != item.get("target_project_id")
            return PullRequestHeadMatch(number=int(item["iid"]), is_fork=is_fork)
        return None

    def close_pull_request(self, pr_number: int) -> None:
        self._put(self._project_url("merge_requests", str(pr_number)), {"state_event": "close"})

    # ---- construction / request helpers ----------------------------------------

    def _headers(self) -> dict[str, str]:
        """`PRIVATE-TOKEN` is omitted when `token` is empty — a public
        project's read endpoints work unauthenticated, the same allowance
        `adapters/github_api.py` makes for `announce --out`."""
        headers = {"Accept": "application/json"}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        return headers

    def _client(self) -> httpx.Client:
        return httpx.Client(headers=self._headers(), timeout=self.timeout)

    def _url(self, *segments: str) -> str:
        return f"{self.base_url}/" + "/".join(quote(segment, safe="") for segment in segments)

    def _project_url(self, *segments: str) -> str:
        return self._url("projects", self.project, *segments)

    def _check(self, response: httpx.Response) -> None:
        check_transient(response, forge=_FORGE)

    def _raise(self, response: httpx.Response) -> None:
        """`response.raise_for_status()`, but the failure leaves this layer as
        an `IndexBotError`. A raw `httpx.HTTPStatusError` walks straight
        through `cli/governance_poll.py`'s per-merge-request guard, which is
        how a single 400 ended a whole governance sweep in production."""
        raise_for_status(response, forge=_FORGE)

    def _paginate(self, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
        with self._client() as client:
            return paginate(client, url, params, forge=_FORGE)

    def _put(self, url: str, payload: dict[str, Any]) -> None:
        with self._client() as client:
            response = client.put(url, json=payload)
        self._check(response)
        self._raise(response)

    def build_action(self, path: str, content: bytes | None, base_sha: str) -> dict[str, Any]:
        """One commit action for `path`. Public only so `commit_files` can call
        it on a *different* instance — the one scoped to the start project,
        which is where the existence probe has to happen."""
        if content is None:
            return {"action": "delete", "file_path": path}
        return {
            "action": "update" if self._file_exists(path, base_sha) else "create",
            "file_path": path,
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        }

    def _file_exists(self, path: str, ref: str) -> bool:
        """`HEAD` on the file endpoint — the metadata-only probe that picks
        `create` vs. `update`, since GitLab has no upsert action."""
        with self._client() as client:
            response = client.head(
                self._project_url("repository", "files", path), params={"ref": ref}
            )
        self._check(response)
        return response.status_code != 404

    def _project_id(self, project: str) -> int:
        with self._client() as client:
            response = client.get(self._url("projects", project))
        self._check(response)
        self._raise(response)
        return int(response.json()["id"])

    def _user_id(self, login: str) -> int:
        matches = self._paginate(self._url("users"), {"username": login})
        if not matches:
            # Mirrors GitHub, where an unknown reviewer login is a 422 that
            # propagates: a maintainer who is not a user is a config bug in
            # `.github/maintainers.yml`, not something to silently drop.
            raise LookupError(f"no {_FORGE} user with username {login!r}")
        return int(matches[0]["id"])

    def _find_open_merge_request(
        self, branch: str, base: str, source_project_id: int | None
    ) -> dict[str, Any] | None:
        """The open MR for `branch` -> `base`, or `None`.

        GitLab's project-level MR list has no `source_project_id` filter, so
        the fork case narrows client-side; without that, two forks pushing
        the same branch name would be indistinguishable.
        """
        candidates = self._paginate(
            self._project_url("merge_requests"),
            {"state": "opened", "source_branch": branch, "target_branch": base},
        )
        for item in candidates:
            same_project = item["source_project_id"] == item["target_project_id"]
            if source_project_id is None:
                if same_project:
                    return item
            elif item["source_project_id"] == source_project_id:
                return item
        return None

    def _find_marked_discussion(self, pr_number: int, marker: str) -> tuple[str, int, str] | None:
        """`(discussion_id, first_note_id, body)` for the thread carrying the
        hidden HTML `marker` — `create_comment`'s idempotency mechanism (G-20:
        one review-required thread per merge request across repeated runs, not
        a fresh one every poll).

        **Two filters, both load-bearing, because the marker is public.** Any
        merge request's author can read the marker out of an earlier thread
        and post it themselves:

        - `resolvable` — a plain note (`POST …/notes`) carries the marker just
          as well as a discussion does, and is *not* resolvable, so it never
          reaches `detailed_merge_status`. Matching one would make
          `create_comment` treat the gate as already open and never create the
          thread that actually blocks the merge. That is the human-review gate
          silently disarmed by a comment.
        - authorship — a resolvable thread opened by the merge request's own
          author is one the author can resolve, which is the same bypass by a
          different route. Only a thread this token opened counts.
        """
        discussions = self._paginate(
            self._project_url("merge_requests", str(pr_number), "discussions"), {}
        )
        for discussion in discussions:
            notes: list[dict[str, Any]] = discussion.get("notes") or []
            if not notes:
                continue
            first = notes[0]
            body = str(first.get("body", ""))
            if marker not in body or not first.get("resolvable"):
                continue
            author: Mapping[str, Any] = first.get("author") or {}
            if author.get("id") != self._self_user_id():
                continue
            return str(discussion["id"]), int(first["id"]), body
        return None

    def _self_user_id(self) -> int | None:
        """The id of the user this token authenticates as, or `None` when the
        adapter is unauthenticated (`announce --out`, public reads).

        `GET /user` is the only endpoint that answers "who am I" on GitLab,
        and the answer is fixed for the life of the token, so it is asked once
        per process."""
        if "self_user_id" not in self._cache:
            self._cache["self_user_id"] = self._fetch_self_user_id()
        user_id: int | None = self._cache["self_user_id"]
        return user_id

    def _fetch_self_user_id(self) -> int | None:
        if not self.token:
            return None
        with self._client() as client:
            response = client.get(self._url("user"))
        self._check(response)
        self._raise(response)
        return int(response.json()["id"])
