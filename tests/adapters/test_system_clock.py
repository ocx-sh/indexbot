"""Tests for `adapters/system_clock.py` — CONTRACTS.md §11."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from indexbot.adapters.system_clock import SystemClock
from indexbot.ports import ClockPort

_ISO8601_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_now_iso8601_matches_utc_shape() -> None:
    clock = SystemClock()
    assert _ISO8601_UTC_RE.fullmatch(clock.now_iso8601()) is not None


def test_now_iso8601_is_current_utc_instant() -> None:
    clock = SystemClock()
    before = datetime.now(UTC)
    value = clock.now_iso8601()
    after = datetime.now(UTC)

    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert before - timedelta(seconds=1) <= parsed <= after + timedelta(seconds=1)


def test_system_clock_conforms_to_clock_port() -> None:
    clock: ClockPort = SystemClock()
    assert _ISO8601_UTC_RE.fullmatch(clock.now_iso8601()) is not None
