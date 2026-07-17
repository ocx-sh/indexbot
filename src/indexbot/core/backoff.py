"""Pure backoff-delay math (ADR-4 G-10; CONTRACTS.md §7).

The retry *loop* — attempt counting, the actual `httpx` call, `time.sleep`,
deciding when `policy.max_attempts` is exhausted and raising
`TransientError` — lives in `adapters/ghcr.py` (imperative shell). This
module only computes *how long* to wait for a given attempt, which is why it
is trivially 100%-coverable without mocking `random`/`time`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Tunables for the retry loop in `adapters/ghcr.py`."""

    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0


def is_retryable_status(status_code: int) -> bool:
    """True for `429` or any `5xx`.

    False for everything else, including other `4xx` — `401` gets one
    token-refresh-and-retry inside `adapters/ghcr.py`, a different mechanism
    from backoff, and `404` is a permanent per-request failure never retried
    by this policy.
    """
    return status_code == 429 or 500 <= status_code < 600


def delay_seconds(
    attempt: int,
    policy: BackoffPolicy,
    *,
    jitter: float,
    retry_after: float | None = None,
) -> float:
    """Seconds to sleep before retrying `attempt` (1-indexed).

    A positive `retry_after` wins outright — the server said exactly how
    long to wait (G-10). Otherwise:
    `min(max_delay_seconds, base_delay_seconds * 2 ** (attempt - 1)) * (0.5 + jitter)`,
    with `jitter` in `[0, 1)` supplied by the caller (`adapters/ghcr.py`
    passes `random.random()`; tests pass a fixed float), keeping this
    function itself deterministic.
    """
    if retry_after is not None and retry_after > 0:
        return retry_after
    exponential = policy.base_delay_seconds * 2 ** (attempt - 1)
    return min(policy.max_delay_seconds, exponential) * (0.5 + jitter)
