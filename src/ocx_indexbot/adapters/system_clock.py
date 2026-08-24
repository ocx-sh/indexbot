"""Wall-clock time — `ClockPort` implementation (CONTRACTS.md §11)."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """`ClockPort` backed by the real wall clock, UTC, second precision."""

    def now_iso8601(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
