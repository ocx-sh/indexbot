"""Port protocols — the seam between `core/` (pure) and `adapters/` (I/O).

Each `Protocol` traces to exactly one `adapters/` module in ADR-4 BD-1's
module map. Method sets are the minimal surface ADR-4's text names for that
adapter; Phase 2 (WP2-C, WP2-D, WP2-G) grows them as `core/observe.py` etc.
need more. None of the adapter modules exist yet — this file only fixes the
interface `core/` will be written against.
"""

from __future__ import annotations

from typing import Protocol


class RegistryPort(Protocol):
    """OCI registry reads. Implemented by `adapters/ghcr.py` (ADR-4 BD-1).

    The bearer-token dance (including retry-on-expired-token) and `tags/list`
    pagination are adapter-internal — `core/` only ever sees the resolved
    shapes below.
    """

    def list_tags(self, repository: str) -> list[str]:
        """Every tag observed on `repository`."""
        ...

    def get_manifest(self, repository: str, reference: str) -> dict[str, object]:
        """Raw manifest or image-index JSON for `reference` (a tag or digest)."""
        ...

    def get_desc_tag_digest(self, repository: str) -> str | None:
        """Digest of the floating `__ocx.desc` tag, or `None` if never published."""
        ...


class GitHubPort(Protocol):
    """GitHub REST/GraphQL calls. Implemented by `adapters/github_api.py`."""

    def get_file_contents(self, path: str, ref: str) -> bytes | None:
        """Contents-API read; `None` if `path` does not exist at `ref`."""
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


class FilePort(Protocol):
    """Path-safe local filesystem access. Implemented by `adapters/local_files.py`."""

    def read_text(self, path: str) -> str | None:
        """`None` if `path` does not exist."""
        ...

    def write_text(self, path: str, content: str) -> None: ...

    def exists(self, path: str) -> bool: ...


class ClockPort(Protocol):
    """Wall-clock time. Implemented by `adapters/system_clock.py`."""

    def now_iso8601(self) -> str:
        """Current UTC instant as an RFC 3339 / ISO 8601 string — the shape
        `TagEntry.observed` and `Yank.at` store."""
        ...
