"""Exit-code contract (ADR-4 BD-2, sysexits family).

Every `indexbot` subcommand exits with exactly one of these four codes; the
`result=` value written to `$GITHUB_OUTPUT` (`cli/_common.py`) tells the
workflow which one applied. Only four outcomes exist — not the full sysexits
catalog — because only four are meaningfully distinct here.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes `indexbot` subcommands use, and only these."""

    OK = 0
    """No-op (nothing to regenerate) or applied (diff computed and committed/PR'd)."""

    VALIDATION_FAILURE = 1
    """A semantic check (`core/validate_entry.py`) rejected the input."""

    ANOMALY = 65
    """Integrity violation requiring a human — never auto-healed."""

    TRANSIENT = 75
    """Backoff exhausted (`core/backoff.py`, G-10) — caller may retry later."""
