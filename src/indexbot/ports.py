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

    from indexbot.model import (
        CommitStatusState,
        ManifestFetch,
        OwnershipProbeResult,
        PullRequestInfo,
    )


class RegistryPort(Protocol):
    """OCI registry reads. Implemented by `adapters/ghcr.py` (ADR-4 BD-1).

    The bearer-token dance (including retry-on-expired-token) and `tags/list`
    pagination are adapter-internal — `core/` only ever sees the resolved
    shapes below. Every method raises `indexbot.errors.TransientError` once
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
        `indexbot.errors.AnomalyError` on mismatch (tamper detection), but
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


class GitHubPort(Protocol):
    """GitHub REST/GraphQL calls. Implemented by `adapters/github_api.py`."""

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
        self, *, branch: str, base_sha: str, message: str, files: Mapping[str, bytes | None]
    ) -> str:
        """Create one atomic commit on `branch` and return the new commit SHA.

        Creates `branch` at `base_sha` first if it does not exist yet (per
        `get_ref_sha`). Uses the Git Data API (tree/commit/ref) — never the
        per-file Contents API — so a multi-file regenerate (root JSON plus N
        observation objects) lands as one commit, not N racing ones. `files`
        maps path -> new content; a `None` value deletes that path.

        Raises `indexbot.errors.TransientError` if `base_sha` is stale (the
        branch moved since it was read) — a concurrent-write race the caller
        may retry, never silently rebased onto a fresh base by the adapter
        itself.
        """
        ...

    def open_or_update_pull_request(self, *, branch: str, base: str, title: str, body: str) -> int:
        """Open a PR for `branch` against `base`, or update the existing one
        for that branch. Returns the PR number either way (idempotent)."""
        ...

    def add_labels(self, pr_number: int, labels: list[str]) -> None:
        """Add `labels` to the PR (classification labels, ADR-4 BD-5)."""
        ...

    def enable_auto_merge(self, pr_number: int) -> None:
        """`enablePullRequestAutoMerge` GraphQL mutation."""
        ...

    def get_pull_request_info(self, pr_number: int) -> PullRequestInfo:
        """Base/head SHAs and changed file paths for `pr_number`, read via
        the GitHub API diff only. `cli/classify_pr.py` never checks out the
        PR head (BD-5's `governance-gate` trust boundary) — this is the one
        call it needs instead. Raises `KeyError` if `pr_number` does not
        exist.
        """
        ...

    def set_commit_status(
        self, sha: str, *, context: str, state: CommitStatusState, description: str
    ) -> None:
        """Set a Commit Status API entry on `sha` — `cli/governance_check.py`'s
        mechanism for the `governance/review-required` required status check
        (BD-5)."""
        ...


class FilePort(Protocol):
    """Path-safe local filesystem access. Implemented by `adapters/local_files.py`.

    Every method raises `indexbot.errors.ValidationError` if `path` (or, for
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
