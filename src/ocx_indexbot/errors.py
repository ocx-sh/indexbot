"""Exception hierarchy mapping to the exit-code contract (ADR-4 BD-2).

`cli/main.py`'s top-level dispatch translates any caught `IndexBotError` into
its `exit_code`. Anything that is *not* an `IndexBotError` — a genuine bug —
is deliberately left to propagate as an unhandled traceback rather than being
caught here.
"""

from __future__ import annotations

from ocx_indexbot.exit_codes import ExitCode


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


class ForgeError(IndexBotError):
    """A forge API call failed in a way this run cannot retry.

    The retryable classes (401, 429, 403+`Retry-After`, 5xx) are already
    `TransientError` by the time this can be raised — see
    `adapters/_http.check_transient`. What is left is a permanent refusal: a
    bare 403, a 404 on a resource the caller was told exists, a 422 from a
    payload the forge would not accept.

    It exists so no `httpx` type ever escapes the adapter layer. That is not
    tidiness: `cli/governance_poll.py` catches `IndexBotError` per merge
    request so one bad MR cannot end the sweep, and a raw
    `httpx.HTTPStatusError` walked straight through that guard in production
    and left every later merge request ungated.

    Exit code 1. BD-2 defines four codes and only one of them means "a hard
    failure the caller must look at"; a forge refusing a write is that, even
    though the input it refused was not the operator's.
    """

    _exit_code = ExitCode.VALIDATION_FAILURE
