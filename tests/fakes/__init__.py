"""In-memory `Protocol` implementations for `indexbot`'s test suite.

Not measured by the coverage gate (`[tool.coverage.run] source = ["src"]` —
these live under `tests/`), but exercised by `tests/fakes/test_fakes.py` so
Phase 2's `core/` test suites can trust them without re-verifying basic
behavior every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from indexbot.ports import ClockPort, FilePort, GitHubPort, RegistryPort


@dataclass
class FakeRegistry:
    """In-memory `RegistryPort` — repository -> tags / manifests / desc digest."""

    tags: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    manifests: dict[tuple[str, str], dict[str, object]] = field(
        default_factory=dict[tuple[str, str], dict[str, object]]
    )
    desc_digests: dict[str, str] = field(default_factory=dict[str, str])

    def list_tags(self, repository: str) -> list[str]:
        return list(self.tags.get(repository, []))

    def get_manifest(self, repository: str, reference: str) -> dict[str, object]:
        try:
            return self.manifests[(repository, reference)]
        except KeyError:
            raise KeyError(f"no manifest for {repository}@{reference}") from None

    def get_desc_tag_digest(self, repository: str) -> str | None:
        return self.desc_digests.get(repository)


@dataclass
class FakeGitHub:
    """In-memory `GitHubPort` — one PR per branch, labels, an auto-merge flag."""

    files: dict[tuple[str, str], bytes] = field(default_factory=dict[tuple[str, str], bytes])
    pull_requests: dict[str, int] = field(default_factory=dict[str, int])
    labels: dict[int, list[str]] = field(default_factory=dict[int, list[str]])
    auto_merge_enabled: set[int] = field(default_factory=set[int])
    _next_pr_number: int = field(default=1, init=False, repr=False)

    def get_file_contents(self, path: str, ref: str) -> bytes | None:
        return self.files.get((path, ref))

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


@dataclass
class InMemoryFiles:
    """In-memory `FilePort` — dict-backed, no real filesystem access."""

    files: dict[str, str] = field(default_factory=dict[str, str])

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content

    def exists(self, path: str) -> bool:
        return path in self.files


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
