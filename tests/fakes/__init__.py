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

from indexbot.errors import TransientError, ValidationError
from indexbot.model import CommitStatusState, ManifestFetch, OwnershipProbeResult, PullRequestInfo
from indexbot.ports import ClockPort, FilePort, GitHubPort, RegistryPort

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
        """Same digest doctrine as `adapters/ghcr.py`: `digest` is computed
        from `raw` (this fake's §1-canonical serialization of the configured
        manifest dict), never a value tests set directly — so a `core/`
        consumer relying on a locally synthesized/trusted digest fails
        against this fake exactly as it would against the real adapter."""
        try:
            manifest = self.manifests[(repository, reference)]
        except KeyError:
            raise KeyError(f"no manifest for {repository}@{reference}") from None
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
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
    """In-memory `GitHubPort` — branches/refs, one PR per branch, labels, statuses, auto-merge.

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
    statuses: dict[str, list[tuple[str, CommitStatusState, str]]] = field(
        default_factory=dict[str, list[tuple[str, CommitStatusState, str]]]
    )
    _next_pr_number: int = field(default=1, init=False, repr=False)
    _next_commit_sha: int = field(default=1, init=False, repr=False)

    def get_file_contents(self, path: str, ref: str) -> bytes | None:
        return self.files.get((path, ref))

    def get_ref_sha(self, ref: str) -> str | None:
        return self.refs.get(ref)

    def commit_files(
        self, *, branch: str, base_sha: str, message: str, files: Mapping[str, bytes | None]
    ) -> str:
        del message  # not modeled — fake tracks resulting file/ref state only
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

    def open_or_update_pull_request(self, *, branch: str, base: str, title: str, body: str) -> int:
        del base, title, body  # not modeled — fake tracks branch -> PR number only
        if branch in self.pull_requests:
            return self.pull_requests[branch]
        number = self._next_pr_number
        self.pull_requests[branch] = number
        self._next_pr_number += 1
        return number

    def add_labels(self, pr_number: int, labels: list[str]) -> None:
        self.labels.setdefault(pr_number, []).extend(labels)

    def enable_auto_merge(self, pr_number: int) -> None:
        self.auto_merge_enabled.add(pr_number)

    def get_pull_request_info(self, pr_number: int) -> PullRequestInfo:
        try:
            return self.pull_request_info[pr_number]
        except KeyError:
            raise KeyError(f"no pull_request_info configured for PR #{pr_number}") from None

    def set_commit_status(
        self, sha: str, *, context: str, state: CommitStatusState, description: str
    ) -> None:
        self.statuses.setdefault(sha, []).append((context, state, description))


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
_github_conforms: GitHubPort = FakeGitHub()
_files_conforms: FilePort = InMemoryFiles()
_clock_conforms: ClockPort = FixedClock()
