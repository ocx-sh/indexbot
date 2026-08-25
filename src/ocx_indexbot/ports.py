"""Port protocols — the seam between `core/` (pure) and `adapters/` (I/O).

Each `Protocol` traces to exactly one `adapters/` module in ADR-4 BD-1's
module map. Method sets were grown in the Contracts stage (Phase 2 prep) to
cover everything the parallel build wave's `core/`/`cli/` modules need —
see `bot/CONTRACTS.md` for the module-by-module rationale. Only types
referenced by a `Protocol` signature below live in `model.py`; everything
else that flows between `core/` modules (e.g. `core/diff.py`'s `Patch`) is
each owning module's own contract, described in `CONTRACTS.md` instead, to
keep this file scoped to the adapter seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ocx_indexbot.model import (
        CommitStatusState,
        ManifestFetch,
        OwnershipProbeResult,
        PullRequestHeadMatch,
        PullRequestInfo,
    )


class RegistryPort(Protocol):
    """OCI registry reads. Implemented by `adapters/registry_v2.py` (ADR-4 BD-1).

    The bearer-token dance (including retry-on-expired-token) and `tags/list`
    pagination are adapter-internal — `core/` only ever sees the resolved
    shapes below. Every method raises `ocx_indexbot.errors.TransientError` once
    `core/backoff.py`'s retry policy is exhausted against 429/5xx registry
    weather (G-10) — `core/` treats that as the one uniform "give up" signal
    regardless of which call failed.
    """

    def list_tags(self, repository: str) -> list[str]:
        """Every tag observed on `repository`.

        Empty list if the repository has no tags; does not distinguish "no
        tags" from "repository does not exist" — existence is established
        separately (a manifest fetch, or `core/validate_entry.py`'s
        allowlist check, which runs before any network call — G-03).
        """
        ...

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        """CAS-verifiable manifest (or image-index) fetch for `reference` on
        `repository`.

        `reference` is a tag name or an OCI-style `sha256:<hex>` digest
        string. Raises `KeyError` if `reference` does not exist on
        `repository` (a 404 response) — used both by `core/observe.py`'s
        per-tag manifest walk and `core/validate_entry.py`'s digest-scope
        check (does a claimed content digest actually resolve on the
        physical repo).

        **Digest doctrine (ADR-1 verifiability chain):** every digest this
        bot records must be derivable from content, never synthesized and
        never trusted from a response header alone — a header can lie, or
        simply be absent. `ManifestFetch.digest` is therefore always
        computed by the implementing adapter as `sha256:<hex>` over
        `ManifestFetch.raw`'s exact wire bytes, never copied verbatim from
        e.g. GHCR's `Docker-Content-Digest` response header. An adapter
        *may* additionally verify a present `Docker-Content-Digest` header
        against its own computed digest and raise
        `ocx_indexbot.errors.AnomalyError` on mismatch (tamper detection), but
        must never substitute the header value for the computed one.
        """
        ...

    def get_desc_tag_digest(self, repository: str) -> str | None:
        """Digest of the floating `__ocx.desc` tag, or `None` if never published."""
        ...

    def get_blob(self, repository: str, digest: str) -> bytes:
        """Raw blob bytes for `digest` (a manifest layer) on `repository`.

        `core/desc.py`'s only way to read the `__ocx.desc` artifact's
        title/description/keywords payload and the readme/logo layers it
        references. Raises `KeyError` if `digest` does not exist on
        `repository`.
        """
        ...

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        """G-15 (ADR-4 carry-forward table) — fetch the physical manifest and
        check whether an embedded canonical identifier equals
        `expected_name` (the entry's logical `name`, e.g.
        `ocx.sh/kitware/cmake`).

        `"confirmed"`: the embedded identifier matches. `"mismatch"`: it
        exists and disagrees — block-tier, `core/validate_entry.py` must
        never treat this as a pass. `"unconfirmed"`: the embedding
        convention/annotation was not found at all — WARN, surfaced on the
        PR, also never a silent pass. The identifier-embedding convention
        itself is unconfirmed against `ocx-mirror`'s actual publishing
        behavior (ADR-4 Risk 2) — this method is a pluggable seam, not a
        fixed annotation-key lookup.
        """
        ...


class ForgePort(Protocol):
    """The hosting forge's API — pull requests, refs, commits, review state.

    Two implementations: `adapters/github_api.py` (REST + one GraphQL
    mutation) and `adapters/gitlab_api.py` (REST v4). The vocabulary below is
    GitHub's because that is where the index was born; every term has an
    exact GitLab counterpart, and the GitLab adapter translates at its own
    boundary rather than leaking a second vocabulary into `core/` and `cli/`:

    | here | GitLab |
    |---|---|
    | pull request | merge request |
    | `pr_number` | MR `iid` (project-scoped, the `!42` number) |
    | comment | note |
    | label | label |
    | reviewer login | user id, resolved by username lookup |
    | auto-merge | `merge_when_pipeline_succeeds` |
    | commit status | commit status (`POST /projects/:id/statuses/:sha`) |

    The one place the two forges differ in *kind* rather than in spelling is
    the privileged trigger: GitHub has `pull_request_target`, GitLab has
    nothing equivalent, so the GitLab governance lane polls the open MRs
    instead of reacting to one. That difference lives in the workflows and in
    the listing method the poller needs, not in the methods below.
    """

    def get_file_contents(self, path: str, ref: str) -> bytes | None:
        """Contents-API read; `None` if `path` does not exist at `ref`."""
        ...

    def get_ref_sha(self, ref: str) -> str | None:
        """The commit SHA `ref` (a branch name) currently points to, or
        `None` if the branch does not exist yet — the signal `commit_files`
        callers use to decide "create a new branch" vs. "fast-forward an
        existing one".
        """
        ...

    def commit_files(
        self,
        *,
        branch: str,
        base_sha: str,
        message: str,
        files: Mapping[str, bytes | None],
        base_repo: str | None = None,
    ) -> str:
        """Create one atomic commit on `branch` and return the new commit SHA.

        Creates `branch` at `base_sha` first if it does not exist yet (per
        `get_ref_sha`).

        `base_repo` names the repository `base_sha` lives in, when that is not
        this one — the announce lane's case, where a fork branch must be cut
        from the *index's* main rather than the fork's own possibly-stale copy.
        GitHub ignores it: a fork network shares object storage there, so the
        upstream SHA is already reachable. **GitLab does not.** Measured
        2026-08-25: a GitLab fork returns `404 Commit Not Found` for its
        upstream's tip and refuses to create a ref at it, so without this the
        announce branch cannot be cut from upstream at all.

        Uses the Git Data API (tree/commit/ref) — never the
        per-file Contents API — so a multi-file regenerate (root JSON plus N
        image indices) lands as one commit, not N racing ones. `files`
        maps path -> new content; a `None` value deletes that path.

        Raises `ocx_indexbot.errors.TransientError` if `base_sha` is stale (the
        branch moved since it was read) — a concurrent-write race the caller
        may retry, never silently rebased onto a fresh base by the adapter
        itself.
        """
        ...

    def open_or_update_pull_request(
        self, *, branch: str, base: str, title: str, body: str, head_repo: str | None = None
    ) -> int:
        """Open a PR for `branch` against `base`, or update the existing one
        for that branch. Returns the PR number either way (idempotent).

        `head_repo` (fork-PR announce revamp): the `<owner>/<repo>` fork the
        branch lives on, when that is not the repo this port is scoped to.
        `cli/announce.py`'s `--fork` mode opens the PR against the index repo
        from a `ForgePort` scoped to the index repo (never the fork), so the
        head branch is somewhere else and has to be named.

        The full path is the port's vocabulary because the two forges need
        different halves of it: GitHub's REST API wants the owner
        (`f"{owner}:{branch}"` as its `head` query), GitLab wants the whole
        project path (the MR is POSTed to the *source* project). Passing the
        owner alone would be a GitHub-shaped contract that GitLab cannot
        satisfy.
        """
        ...

    def add_labels(self, pr_number: int, labels: list[str]) -> None:
        """Add `labels` to the PR (classification labels, ADR-4 BD-5)."""
        ...

    def enable_auto_merge(self, pr_number: int, *, head_sha: str) -> None:
        """Arm the forge's own auto-merge for `pr_number`, bound to `head_sha`.

        `head_sha` is not decoration. Arming happens after a gate that judged
        one particular revision, and the two are separate round-trips: between
        them the author can push. Both forges provide the guard — GitHub's
        mutation takes `expectedHeadOid`, GitLab's merge call takes `sha` —
        and an adapter that omits it arms against whatever the head happens to
        be at call time, which is a revision nothing classified, validated or
        gated.

        A head that has moved is **not** an error. It means the decision is
        stale, the next poll tick will re-classify the new revision, and this
        call must return quietly rather than take a sweep down with it.

        **Already mergeable is not an error either — it is a merge.** Arming
        is only possible while the merge is still blocked, so when every
        required check finished before this call got here, GitHub refuses the
        mutation outright and the machine-lane PR would wait forever for an
        auto-merge nobody armed. An adapter must perform the equivalent merge
        itself in that case, bound to the same `head_sha` and with no
        privilege the armed route would not have had. GitLab needs no special
        case: `merge_when_pipeline_succeeds` on an already-succeeded pipeline
        merges immediately by construction.
        """
        ...

    def withdraw_auto_merge(self, pr_number: int) -> None:
        """Idempotently take back whatever `enable_auto_merge` armed for
        `pr_number`, if anything.

        The other half of the arm/withdraw pair `cli/governance_gate.py`'s
        single-PR gate owns end to end (folded in from
        `.github/workflows/governance.yml`'s separate `arm-auto-merge` job,
        "Withdraw auto-merge — human lane" step): read whether auto-merge is
        currently armed, and only call the disabling operation if it is. A PR
        that was never armed — the ordinary human-lane case — must be a
        cheap no-op, not an error; both forges reject disabling what was
        never enabled, so the read-then-act shape is load-bearing here, not
        an optimization.

        Unlike `enable_auto_merge`, this takes no `head_sha`. Withdrawing is
        not a decision about a revision — it is "this PR is not machine-lane
        any more, for whatever revision that turns out to be" — so there is
        nothing to bind it to.
        """
        ...

    def get_pull_request_info(self, pr_number: int) -> PullRequestInfo:
        """Base/head SHAs, changed file paths, and author identity for
        `pr_number`, read via the GitHub API diff only. `cli/classify_pr.py`
        never checks out the PR head (BD-5's `governance-gate` trust
        boundary) — this is the one call it needs instead.
        `PullRequestInfo.author_login`/`.author_id` (G-19) are
        `cli/governance_check.py`'s only reason to need this beyond
        `classify_pr.py`'s own use. Raises `KeyError` if `pr_number` does not
        exist.

        **The head sha and the changed paths must describe the same
        revision.** Neither forge serves them from one endpoint, so an
        implementation reads at least twice and a push can land in between —
        returning one revision's file list under another's sha. Everything
        downstream then compounds it: the classification is of content that is
        not there, and `cli/governance_gate.py` arms auto-merge bound to
        `head_sha`, which still resolves, so the arm's own head guard passes.
        An implementation must therefore re-read the head after the file walk
        and raise `ocx_indexbot.errors.TransientError` if it moved — the next
        tick is the retry (ADR-4 BD-2). Both real adapters do; `tests/fakes`'
        in-memory stand-in does not model it, because a dict has no round
        trips to race.
        """
        ...

    def set_commit_status(
        self,
        sha: str,
        *,
        context: str,
        state: CommitStatusState,
        description: str,
        pull_request: int | None = None,
    ) -> None:
        """Set a commit status on `sha` — `cli/governance_check.py`'s report of
        the `governance/review-required` decision (BD-5).

        A **report**, and on GitLab only that. Measured on gitlab.com Free
        (2026-08-24 and 2026-08-25):

        - A same-project merge request: an external status under "pipelines
          must succeed" drives `detailed_merge_status` and makes `PUT …/merge`
          refuse with 405 while it is `pending` or `failed`. Fail-closed with
          no status at all.
        - A **fork** merge request: it does not gate. The merge request's
          `head_pipeline` is the *fork's* pipeline, and a status posted in the
          parent creates a pipeline the merge request is not associated with —
          `detailed_merge_status` stays `mergeable`. Worse, that fork pipeline
          is fork-authored, so treating it as evidence would put the parent's
          merge gate under the fork's control.

        So on GitLab the thing that actually blocks is
        `create_comment`'s unresolved thread; see there. This method still
        runs on both forges and for every merge request, because the status is
        what a human reads.

        `pull_request` is the merge request this status is about. GitHub does
        not need it — a fork PR's head is reachable from the base repository.
        GitLab does: the commit lives in the fork, and its status API takes a
        `ref`, which the adapter builds from this number. Omitting it on
        GitLab makes a fork merge request's status a 404.
        """
        ...

    def request_reviewers(self, pr_number: int, logins: list[str]) -> None:
        """Assign `logins` as reviewers on `pr_number` (G-20 — non-owner/
        human-lane PRs get reviewers from `.github/maintainers.yml`).
        `cli/governance_check.py` filters the PR author out of `logins`
        before calling this — assigning a PR's own author as their own
        reviewer is a GitHub API error, never this port's job to guard
        against."""
        ...

    def create_comment(self, pr_number: int, body: str, *, marker: str) -> None:
        """Post `body` as an issue/PR comment on `pr_number`, idempotently:
        update the existing comment in place if one already contains the
        hidden HTML `marker` (e.g. `<!-- indexbot:governance -->`), skip
        entirely if that comment's body is already exactly `body`, else
        create a new one. `cli/governance_check.py`'s G-20 mechanism for a
        single, non-spamming review-required comment across repeated runs.

        **On GitLab this is also the merge gate**, which is why the two forges'
        implementations are not the same kind of object. A GitLab
        implementation opens a *discussion* — a resolvable thread — and an
        unresolved one makes `detailed_merge_status` report
        `discussions_not_resolved` under the project's "All threads must be
        resolved" setting. That is Free-tier, parent-controlled, and works for
        a fork merge request, which the commit status does not. GitHub's is a
        plain issue comment; its gate is the commit status plus branch
        protection.
        """
        ...

    def resolve_review_thread(self, pr_number: int, *, marker: str) -> None:
        """Release whatever `create_comment` left blocking, if anything.

        GitLab resolves the marked discussion, which is what lets the merge
        request merge. GitHub has nothing to do: an issue comment blocks
        nothing there, and the commit status already carries the decision.
        Never creates a thread — a green merge request that never needed
        review must not acquire a comment saying so.
        """
        ...

    def list_approvals(self, pr_number: int, *, head_sha: str) -> tuple[int, ...]:
        """Numeric user **ids** that have approved `pr_number` **at `head_sha`**,
        ascending.

        The human lane's exit. Without it a human-lane PR has no machine-
        readable "a person said yes", and on a forge where the commit status
        IS the merge gate that is not a stalled PR — it is a permanently
        unmergeable one.

        **Ids, never logins**, and that is an authorization decision, not a
        spelling one. An approval outranks every disposition this bot can
        reach, `governance.auto_merge = never` included, so whoever this
        returns decides whether a change merges without a second person. A
        login is renameable and, once released, recyclable: match on one and a
        stranger who acquires a former maintainer's name inherits their veto
        over the human lane. `model.Owner.github_id` exists for exactly this
        reason and `cli/governance_check._author_owns_every_touched_package`
        already binds on it — this method is the other half. Both forges hand
        the id back beside the login (GitHub's review `user.id`, GitLab's
        `approved_by[].user.id` and the `approved` event's `author.id`), so
        nothing is lost by carrying the id.

        Reviewer *assignment* is the one place that still travels by login —
        GitHub's `request_reviewers` API takes names, not ids. The two are
        deliberately not the same field: `request_reviewers` asks a person to
        look, this reports that one did.

        `head_sha` is what makes an approval mean something. GitHub records
        the commit each review was left on, so a stale approval is filtered
        out here. GitLab's approval objects carry no commit and its
        "Remove all approvals when commits are added" setting is Premium, so
        that adapter reconstructs the same answer from two server-generated
        timestamps instead — see `adapters/gitlab_api.list_approvals`.
        """
        ...

    def list_open_pull_requests(self) -> tuple[int, ...]:
        """Every open PR/MR number on this repository, ascending.

        The GitLab governance lane's entry point (`cli/governance_poll.py`).
        GitLab has no `pull_request_target` — a fork MR's pipeline runs in the
        fork, with the fork's variables, and the only way to put the parent's
        variables on a fork MR is a parent pipeline that *executes the fork's
        `.gitlab-ci.yml`*, which is the footgun `pull_request_target` exists
        to avoid. So the privileged actor there cannot be MR-event-driven: it
        is a schedule on the parent's default branch that asks this question
        and gates what it finds.

        GitHub implements it too, and not only for symmetry — it is what makes
        the poll lane testable and runnable against either forge, and what a
        GitHub deployment would use if it ever wanted a sweep that re-gates
        PRs whose base moved under them.
        """
        ...

    def create_or_update_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> int:
        """Idempotent per exact `title` match among open, non-PR issues:
        create one if none exists, else update its body (only if it
        actually differs) and return the existing number unchanged.
        `cli/reconcile.py`'s anomaly-report mechanism — promoted onto this
        Protocol (fork-PR announce revamp; previously an
        `adapters/github_api.py`-only capability, flagged as a
        `ports.ForgePort` gap in CONTRACTS.md §13 item 4)."""
        ...

    def find_pull_request_by_head_sha(self, head_sha: str) -> PullRequestHeadMatch | None:
        """The open PR/MR whose CURRENT head commit is exactly `head_sha`, or
        `None` if none has it (WP5-C, `cli/label_failed_run.py`).

        A `workflow_run`-triggered job (GitHub) or a scheduled sweep (GitLab)
        holds base-repo privileges regardless of who authored the workflow
        that just completed, and for exactly that reason never checks out
        anything — the head commit is the one fact it is handed, and it must
        turn that back into a pull request through the API alone.

        Both forges answer "which pull requests is this commit associated
        with", not "which pull request currently has this commit as its
        head" — a commit can be part of more than one PR's history (a
        rebase or a merge chain), and a PR whose author has since pushed
        again still lists its OLD head commits among its associated ones.
        Returning the first API result unfiltered could therefore label an
        unrelated PR, or a PR the failing run is no longer even about. This
        method filters to an EXACT head-sha match before returning anything —
        the same discipline `pr-checks-label.yml`'s own `jq` filter applied
        (`select(.head.sha == $sha)`), just moved into the port. A moved
        head is not an error, it is simply "no match": the caller reports
        nothing to do rather than guessing.
        """
        ...

    def close_pull_request(self, pr_number: int) -> None:
        """Close `pr_number` without merging it.

        `indexbot stale`'s terminal action (WP5-C) for a PR that has carried
        `checks-failed` long enough with no activity to also go through
        `checks-failed-stale` — ADR-6 FP-8's spam posture, second half.
        """
        ...


class FilePort(Protocol):
    """Path-safe local filesystem access. Implemented by `adapters/local_files.py`.

    Every method raises `ocx_indexbot.errors.ValidationError` if `path` (or, for
    `list_files`, `prefix`) contains `..` or is absolute — defense in depth
    even though callers are expected to only ever pass already-validated
    relative paths (ADR-4 BD-4's untrusted-input discipline applies to path
    construction generally, not only to the two package-id/repository
    regexes).
    """

    def read_text(self, path: str) -> str | None:
        """`None` if `path` does not exist."""
        ...

    def write_text(self, path: str, content: str) -> None: ...

    def read_bytes(self, path: str) -> bytes | None:
        """`None` if `path` does not exist — binary counterpart to
        `read_text`, used for CAS blobs (`.svg`/`.png` logos)."""
        ...

    def write_bytes(self, path: str, content: bytes) -> None: ...

    def exists(self, path: str) -> bool: ...

    def list_files(self, prefix: str) -> list[str]:
        """Every file (not directory) path under `prefix`, relative to the
        same root every other `FilePort` method uses, sorted. Empty list if
        `prefix` does not exist. `core/render.py`'s reachability walk and
        `cli/reconcile.py`'s full-index enumeration both start here.
        """
        ...


class ClockPort(Protocol):
    """Wall-clock time. Implemented by `adapters/system_clock.py`."""

    def now_iso8601(self) -> str:
        """Current UTC instant as an RFC 3339 / ISO 8601 string — the shape
        `TagEntry.observed` and `Yank.at` store."""
        ...


class GitPort(Protocol):
    """Read-only queries against the checked-out repository's own git history.
    Implemented by `adapters/local_git.py`.

    Exists for exactly one caller — `cli/validate_pr.py`, which needs the
    file set a pull request authored and each of those files' bytes as they
    stand on the base ref. Both used to be shell steps in the generated
    pipeline; the two properties that make them safe (a `:(glob)` pathspec,
    a three-dot range) are security-relevant enough to be tested, and YAML is
    not testable.

    Every method raises `ocx_indexbot.errors.ValidationError` when `git`
    itself fails — a base ref that was never fetched (`fetch-depth: 0`
    missing) is the failure this actually catches in the field, and it is a
    hard, non-retryable configuration error rather than weather.
    """

    def changed_package_roots(self, base_sha: str, *, root_glob: str) -> tuple[str, ...]:
        """Repository-relative paths of every package root this branch
        added or modified relative to `base_sha`, in git's own order.

        Deletes are excluded — there is nothing to validate about a removed
        root — but every other status is included, symlink-swaps included.
        `root_glob` is `core/policy.root_glob(name_segments)`, applied as a
        `:(glob)` pathspec so it can never select a CAS object.
        """
        ...

    def file_at(self, ref: str, path: str) -> bytes | None:
        """`path`'s exact bytes at `ref`, or `None` when it does not exist
        there — the "did this root already exist on the base ref?" question
        ADR-2 ND-4 turns on."""
        ...
