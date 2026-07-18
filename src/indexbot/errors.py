"""Exception hierarchy mapping to the exit-code contract (ADR-4 BD-2).

`cli/main.py`'s top-level dispatch translates any caught `IndexBotError` into
its `exit_code`. Anything that is *not* an `IndexBotError` — a genuine bug —
is deliberately left to propagate as an unhandled traceback rather than being
caught here.
"""

from __future__ import annotations

from indexbot.exit_codes import ExitCode


class IndexBotError(Exception):
    """Base for every error `indexbot` raises deliberately.

    Subclasses override `_exit_code`; `exit_code` is the read-only property
    `cli/main.py` reads to decide the process exit code.
    """

    _exit_code: ExitCode = ExitCode.VALIDATION_FAILURE

    def __init__(self, message: str) -> None:
        super().__init__(message)

    @property
    def exit_code(self) -> ExitCode:
        """The exit code `cli/main.py`'s top-level handler should exit with."""
        return self._exit_code


class ValidationError(IndexBotError):
    """A semantic check failed (`core/validate_entry.py`)."""

    _exit_code = ExitCode.VALIDATION_FAILURE


class AnomalyError(IndexBotError):
    """An integrity violation requiring a human — never auto-healed (ADR-4 BD-2)."""

    _exit_code = ExitCode.ANOMALY


class TransientError(IndexBotError):
    """Backoff exhausted (`core/backoff.py`, G-10) — caller may retry later."""

    _exit_code = ExitCode.TRANSIENT
