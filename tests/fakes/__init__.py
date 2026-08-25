"""In-memory `Protocol` implementations for `indexbot`'s test suite.

Not measured by the coverage gate (`[tool.coverage.run] source = ["src"]` —
these live under `tests/`), but exercised by `tests/fakes/test_fakes.py` so
Phase 2's `core/` test suites can trust them without re-verifying basic
behavior every time.

Some methods model real multi-step state transitions (e.g. `FakeGitHub`'s
`commit_files`/`get_ref_sha` pair simulates optimistic-concurrency branch
updates); others are purely canned-response lookups configured at
construction time (e.g. `FakeGitHub.pull_request_info`,
`FakeRegistry.ownership`) because faithfully deriving them from the fake's
other state (a full git-diff or a real ownership-annotation model) would
buy `core/` test suites nothing over just setting the answer directly.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field

from ocx_indexbot.core.policy import (
    DEFAULT_RECONCILE_CRON,
    DEFAULT_STALE_CRON,
    CiConfig,
    IndexPolicy,
)
from ocx_indexbot.errors import TransientError, ValidationError
from ocx_indexbot.model import (
    CommitStatusState,
    ManifestFetch,
    OwnershipProbeResult,
    PullRequestHeadMatch,
    PullRequestInfo,
)
from ocx_indexbot.ports import ClockPort, FilePort, ForgePort, RegistryPort

_VIRTUAL_ROOT = "/__in_memory_root__"


def _resolve(path: str) -> str:
    """Mirrors `adapters/local_files.py::LocalFiles._resolve`'s path-
    containment check without touching a real filesystem — `InMemoryFiles`
    has no directory tree or symlinks to resolve, only `..`/absolute-path
    traversal is representable at all for an in-memory dict store.

    `path` is joined onto a fixed virtual root and normalized
    (`posixpath.normpath`); a `..` climbing past the root, or an absolute
    `path` (which `posixpath.join` treats as replacing the root entirely),
    lands the result outside `_VIRTUAL_ROOT` and raises `ValidationError` —
    before `InMemoryFiles`' backing dict is ever touched. This exists so
    `core`/`cli` test suites exercising only the fake still catch an
    unsanitized `FilePort` path, the same class of bug
    `tests/adapters/test_local_files.py`'s traversal matrix guards against
    for the real adapter.

    Returns the normalized, root-relative path — every `InMemoryFiles`
    method uses this as its backing-dict key rather than the raw
    caller-supplied `path`, so an in-bounds `..` segment (e.g.
    `"p/kitware/../kitware/cmake.json"`) resolves to the same key a clean
    path would, matching `LocalFiles`' real resolve-then-access behavior.
    """
    resolved = posixpath.normpath(posixpath.join(_VIRTUAL_ROOT, path))
    if resolved != _VIRTUAL_ROOT and not resolved.startswith(f"{_VIRTUAL_ROOT}/"):
        raise ValidationError(f"path escapes root: {path!r}")
    return "" if resolved == _VIRTUAL_ROOT else resolved.removeprefix(f"{_VIRTUAL_ROOT}/")


@dataclass
class FakeRegistry:
    """In-memory `RegistryPort` — repository -> tags/manifests/blobs/desc/ownership."""

    tags: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    manifests: dict[tuple[str, str], dict[str, object]] = field(
        default_factory=dict[tuple[str, str], dict[str, object]]
    )
    desc_digests: dict[str, str] = field(default_factory=dict[str, str])
    blobs: dict[tuple[str, str], bytes] = field(default_factory=dict[tuple[str, str], bytes])
    ownership: dict[str, OwnershipProbeResult] = field(
        default_factory=dict[str, OwnershipProbeResult]
    )

    def list_tags(self, repository: str) -> list[str]:
        return list(self.tags.get(repository, []))

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        """Same digest doctrine as `adapters/registry_v2.py`: `digest` is computed
        from `raw`, never a value tests set directly — so a `core/` consumer
        relying on a locally synthesized/trusted digest fails against this
        fake exactly as it would against the real adapter.

        `raw` is **deliberately not any encoder's canonical output**: 2-space
        indent, the configured mapping's own insertion key order. A registry
        serves the bytes its publisher pushed and the index stores those bytes
        verbatim, so a consumer that re-encoded a parsed manifest must be
        byte-distinguishable here from one that copied `raw`. Emitting
        `json.dumps(..., sort_keys=True, separators=(",", ":"))` instead would
        make the two indistinguishable and turn every "stored verbatim"
        assertion written against this fake into a tautology
        (`test_fake_registry_manifest_bytes_are_not_canonical_json` guards it).
        """
        try:
            manifest = self.manifests[(repository, reference)]
        except KeyError:
            raise KeyError(f"no manifest for {repository}@{reference}") from None
        raw = json.dumps(manifest, indent=2, ensure_ascii=True).encode("utf-8")
        digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        return ManifestFetch(raw=raw, digest=digest, parsed=manifest)

    def get_desc_tag_digest(self, repository: str) -> str | None:
        return self.desc_digests.get(repository)

    def get_blob(self, repository: str, digest: str) -> bytes:
        try:
            return self.blobs[(repository, digest)]
        except KeyError:
            raise KeyError(f"no blob {digest} for {repository}") from None

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        del expected_name  # fake ignores the expected value — canned result keyed by repository
        return self.ownership.get(repository, "unconfirmed")


@dataclass
class FakeGitHub:
    """In-memory `ForgePort` — branches/refs, one PR per branch, labels, statuses, auto-merge.

    `pull_request_info` is canned (not derived from `files`/`refs`) — see
    module docstring.
    """

    files: dict[tuple[str, str], bytes] = field(default_factory=dict[tuple[str, str], bytes])
    refs: dict[str, str] = field(default_factory=dict[str, str])
    pull_requests: dict[str, int] = field(default_factory=dict[str, int])
    pull_request_info: dict[int, PullRequestInfo] = field(
        default_factory=dict[int, PullRequestInfo]
    )
    labels: dict[int, list[str]] = field(default_factory=dict[int, list[str]])
    auto_merge_enabled: set[int] = field(default_factory=set[int])
    auto_merge_head_sha: dict[int, str] = field(default_factory=dict[int, str])
    """The revision each auto-merge arm was bound to — both real adapters pass
    it to the forge as an optimistic-concurrency guard, so a caller that arms
    against a stale head is a bug the fake has to be able to show."""
    statuses: dict[str, list[tuple[str, CommitStatusState, str]]] = field(
        default_factory=dict[str, list[tuple[str, CommitStatusState, str]]]
    )
    requested_reviewers: dict[int, list[str]] = field(default_factory=dict[int, list[str]])
    status_error: Exception | None = None
    """Raised by `set_commit_status`, to model the one forge failure the gate
    has to survive in the right direction: GitLab refusing a status
    transition it has no edge for."""

    calls: list[str] = field(default_factory=list[str])
    """Ordered names of the write methods that were called.

    The gate's *order* is a safety property — the blocking artifact goes up
    before the status and comes down only after it — and order is the one
    thing a set of recorded effects cannot show.
    """

    comments: dict[int, dict[str, str]] = field(default_factory=dict[int, dict[str, str]])
    """pr_number -> {marker: body} — mirrors `GitHubApi.create_comment`'s
    one-comment-per-marker idempotency without modeling a full ordered
    comment thread (no `core/`/`cli/` consumer needs anything beyond "what's
    the current body under this marker")."""
    cross_repo_bases: dict[str, str] = field(default_factory=dict[str, str])
    """branch -> the repository its base SHA was said to live in. Recorded so
    a test can assert the announce lane tells a fork where upstream is, which
    on GitLab is the difference between a branch and a 404."""
    unresolved: dict[int, set[str]] = field(default_factory=dict[int, set[str]])
    """pr_number -> markers whose thread is still blocking. Meaningless on
    GitHub, where nothing resolves; modeled anyway because `core`/`cli` call
    the same method on both and the difference belongs in the adapters."""
    issues: dict[str, tuple[int, str]] = field(default_factory=dict[str, tuple[int, str]])
    """title -> (number, body), for `create_or_update_issue`'s
    idempotent-per-exact-title fake."""
    head_sha_lookup: dict[str, PullRequestHeadMatch] = field(
        default_factory=dict[str, PullRequestHeadMatch]
    )
    """head_sha -> the exact-match answer `find_pull_request_by_head_sha`
    should give — canned, like `pull_request_info`: faithfully modeling a
    commit's full PR-association history (the thing the real adapters filter
    down from) would buy `cli/label_failed_run.py`'s test suite nothing over
    just setting the already-filtered answer directly. A SHA absent here is
    "no PR currently has this SHA as its head" — both the plain-miss case and
    the "head moved on since the run started" case look identical from the
    caller's side, which is also true of the real adapters."""
    closed_pull_requests: set[int] = field(default_factory=set[int])
    """Every `close_pull_request` call — `indexbot stale`'s terminal action."""
    _next_pr_number: int = field(default=1, init=False, repr=False)
    _next_commit_sha: int = field(default=1, init=False, repr=False)
    _next_issue_number: int = field(default=1, init=False, repr=False)

    def get_file_contents(self, path: str, ref: str) -> bytes | None:
        return self.files.get((path, ref))

    def get_ref_sha(self, ref: str) -> str | None:
        return self.refs.get(ref)

    def commit_files(
        self,
        *,
        branch: str,
        base_sha: str,
        message: str,
        files: Mapping[str, bytes | None],
        base_repo: str | None = None,
    ) -> str:
        del message  # not modeled — fake tracks resulting file/ref state only
        if base_repo is not None:
            self.cross_repo_bases[branch] = base_repo
        current = self.refs.get(branch)
        if current is not None and current != base_sha:
            raise TransientError(f"branch {branch} moved since base_sha {base_sha} was read")
        for path, content in files.items():
            if content is None:
                self.files.pop((path, branch), None)
            else:
                self.files[(path, branch)] = content
        new_sha = f"sha-{self._next_commit_sha}"
        self._next_commit_sha += 1
        self.refs[branch] = new_sha
        return new_sha

    def open_or_update_pull_request(
        self, *, branch: str, base: str, title: str, body: str, head_repo: str | None = None
    ) -> int:
        del base, title, body  # not modeled — fake tracks head -> PR number only
        head = branch if head_repo is None else f"{head_repo}:{branch}"
        if head in self.pull_requests:
            return self.pull_requests[head]
        number = self._next_pr_number
        self.pull_requests[head] = number
        self._next_pr_number += 1
        return number

    def add_labels(self, pr_number: int, labels: list[str]) -> None:
        self.labels.setdefault(pr_number, []).extend(labels)

    def enable_auto_merge(self, pr_number: int, *, head_sha: str) -> None:
        self.auto_merge_enabled.add(pr_number)
        self.auto_merge_head_sha[pr_number] = head_sha

    def withdraw_auto_merge(self, pr_number: int) -> None:
        """`discard`/`pop(..., None)`, not `remove`/`del` — withdrawing must
        be a no-op on a PR that was never armed, the ordinary human-lane
        case, matching both real adapters' read-then-act shape."""
        self.auto_merge_enabled.discard(pr_number)
        self.auto_merge_head_sha.pop(pr_number, None)

    def get_pull_request_info(self, pr_number: int) -> PullRequestInfo:
        try:
            return self.pull_request_info[pr_number]
        except KeyError:
            raise KeyError(f"no pull_request_info configured for PR #{pr_number}") from None

    def set_commit_status(
        self,
        sha: str,
        *,
        context: str,
        state: CommitStatusState,
        description: str,
        pull_request: int | None = None,
    ) -> None:
        del pull_request  # the fake needs no ref to place a commit on
        self.calls.append("set_commit_status")
        if self.status_error is not None:
            raise self.status_error
        self.statuses.setdefault(sha, []).append((context, state, description))

    def request_reviewers(self, pr_number: int, logins: list[str]) -> None:
        self.requested_reviewers.setdefault(pr_number, []).extend(logins)

    def create_comment(self, pr_number: int, body: str, *, marker: str) -> None:
        self.calls.append("create_comment")
        self.comments.setdefault(pr_number, {})[marker] = body
        self.unresolved.setdefault(pr_number, set()).add(marker)

    def resolve_review_thread(self, pr_number: int, *, marker: str) -> None:
        self.calls.append("resolve_review_thread")
        self.unresolved.get(pr_number, set()).discard(marker)

    approvals: dict[int, list[int]] = field(default_factory=dict[int, list[int]])
    """pr_number -> approving user **ids** (`ForgePort.list_approvals` returns
    ids, never logins, so that authorization binds on something a rename
    cannot move). Not SHA-keyed: the fake models the forge-independent answer,
    and each adapter's own suite owns how its forge decides an approval still
    applies. It likewise does not model the head-moved re-read
    `get_pull_request_info` performs on both real adapters — that is a
    two-round-trip race, and a dict has no round trips."""

    def list_approvals(self, pr_number: int, *, head_sha: str) -> tuple[int, ...]:
        del head_sha
        return tuple(sorted(self.approvals.get(pr_number, [])))

    def list_open_pull_requests(self) -> tuple[int, ...]:
        """Every PR the fake has been given info for — `pull_request_info` is
        canned, so "configured" is the only meaning "open" can have here."""
        return tuple(sorted(self.pull_request_info))

    def create_or_update_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> int:
        del labels  # not modeled — fake tracks title -> (number, body) only
        existing = self.issues.get(title)
        number = existing[0] if existing is not None else self._next_issue_number
        if existing is None:
            self._next_issue_number += 1
        self.issues[title] = (number, body)
        return number

    def find_pull_request_by_head_sha(self, head_sha: str) -> PullRequestHeadMatch | None:
        return self.head_sha_lookup.get(head_sha)

    def close_pull_request(self, pr_number: int) -> None:
        self.closed_pull_requests.add(pr_number)


@dataclass
class InMemoryFiles:
    """In-memory `FilePort` — dict-backed, no real filesystem access.

    Stores everything as `bytes`; `read_text`/`write_text` encode/decode
    UTF-8 at the boundary so text and binary (CAS logo blobs) callers share
    one backing store. Every method rejects a `..`/absolute-path-traversing
    `path` (or, for `list_files`, `prefix`) the same way
    `adapters/local_files.py::LocalFiles` does — see `_resolve`.
    """

    files: dict[str, bytes] = field(default_factory=dict[str, bytes])

    def read_text(self, path: str) -> str | None:
        content = self.files.get(_resolve(path))
        return None if content is None else content.decode("utf-8")

    def write_text(self, path: str, content: str) -> None:
        self.files[_resolve(path)] = content.encode("utf-8")

    def read_bytes(self, path: str) -> bytes | None:
        return self.files.get(_resolve(path))

    def write_bytes(self, path: str, content: bytes) -> None:
        self.files[_resolve(path)] = content

    def exists(self, path: str) -> bool:
        return _resolve(path) in self.files

    def list_files(self, prefix: str) -> list[str]:
        resolved_prefix = _resolve(prefix)
        normalized = (
            resolved_prefix
            if resolved_prefix == "" or resolved_prefix.endswith("/")
            else f"{resolved_prefix}/"
        )
        return sorted(p for p in self.files if p.startswith(normalized))


@dataclass
class FixedClock:
    """`ClockPort` returning a fixed instant, for deterministic tests."""

    fixed: str = "2026-07-17T00:00:00Z"

    def now_iso8601(self) -> str:
        return self.fixed


# Structural-conformance check: fails at import time (pyright) or class
# instantiation (runtime) if a fake drifts from its Protocol's method set.
_registry_conforms: RegistryPort = FakeRegistry()
_github_conforms: ForgePort = FakeGitHub()
_files_conforms: FilePort = InMemoryFiles()
_clock_conforms: ClockPort = FixedClock()

# --- deployment policy -------------------------------------------------------


def make_policy(**overrides: object) -> IndexPolicy:
    """The public index's own policy, as a starting point for any test that
    needs one.

    Two segments, `ocx.sh`, `ghcr.io` — so a suite written before 0.2.0 made
    the index's identity configurable keeps asserting on exactly the values it
    always did. Override a field to describe a *different* deployment:
    `make_policy(name_segments=3)`, `make_policy(name="acme.corp")`. The
    `ci` block's own fields are overridden by name too (`make_policy(
    forge="gitlab")`, `make_policy(owner="acme")`) — they are nested on the
    real dataclass, but a test that cares about one of them should not have
    to build the other five.
    """
    ci_fields: dict[str, object] = {
        "forge": "github",
        "owner": "ocx-sh",
        "run": "uv run --project bot-tools --frozen -- indexbot",
        "setup": "./.github/actions/setup-bot",
        "deploy_workflow": "",
        "reconcile_cron": DEFAULT_RECONCILE_CRON,
        "stale_cron": DEFAULT_STALE_CRON,
    }
    fields: dict[str, object] = {
        "name": "ocx.sh",
        "name_segments": 2,
        "registry_hosts": frozenset({"ghcr.io"}),
        "reserved_namespaces": frozenset({"ocx", "ocx-sh", "ocx-contrib", "ocx-rs"}),
        "auto_merge": "owners",
    }
    for key, value in overrides.items():
        target = ci_fields if key in ci_fields else fields
        target[key] = value
    fields["ci"] = CiConfig(**ci_fields)  # pyright: ignore[reportArgumentType]
    return IndexPolicy(**fields)  # pyright: ignore[reportArgumentType]
