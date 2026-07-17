from __future__ import annotations

from indexbot.core.backoff import BackoffPolicy, delay_seconds, is_retryable_status


def test_backoff_policy_defaults() -> None:
    policy = BackoffPolicy()
    assert policy.max_attempts == 5
    assert policy.base_delay_seconds == 1.0
    assert policy.max_delay_seconds == 30.0


def test_is_retryable_status_429() -> None:
    assert is_retryable_status(429) is True


def test_is_retryable_status_5xx_range() -> None:
    assert is_retryable_status(500) is True
    assert is_retryable_status(599) is True


def test_is_retryable_status_non_retryable() -> None:
    assert is_retryable_status(200) is False
    assert is_retryable_status(401) is False
    assert is_retryable_status(404) is False
    assert is_retryable_status(428) is False
    assert is_retryable_status(600) is False


def test_delay_seconds_retry_after_wins_over_formula() -> None:
    policy = BackoffPolicy()
    # A huge attempt/jitter would normally cap at max_delay_seconds — proves
    # retry_after short-circuits the formula entirely (G-10).
    assert delay_seconds(99, policy, jitter=0.99, retry_after=42.0) == 42.0


def test_delay_seconds_retry_after_zero_is_ignored() -> None:
    policy = BackoffPolicy(base_delay_seconds=2.0, max_delay_seconds=1000.0)
    result = delay_seconds(1, policy, jitter=0.0, retry_after=0.0)
    assert result == 2.0 * 0.5


def test_delay_seconds_retry_after_negative_is_ignored() -> None:
    policy = BackoffPolicy(base_delay_seconds=2.0, max_delay_seconds=1000.0)
    result = delay_seconds(1, policy, jitter=0.0, retry_after=-5.0)
    assert result == 2.0 * 0.5


def test_delay_seconds_retry_after_none_uses_formula() -> None:
    policy = BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=1000.0)
    result = delay_seconds(2, policy, jitter=0.0)
    assert result == 2.0 * 0.5  # base * 2**(2-1) = 2.0, * (0.5 + 0)


def test_delay_seconds_exponential_growth_by_attempt() -> None:
    policy = BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=1000.0)
    assert delay_seconds(1, policy, jitter=0.0) == 1.0 * 0.5
    assert delay_seconds(2, policy, jitter=0.0) == 2.0 * 0.5
    assert delay_seconds(3, policy, jitter=0.0) == 4.0 * 0.5


def test_delay_seconds_capped_at_max_delay_seconds() -> None:
    policy = BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=5.0)
    result = delay_seconds(10, policy, jitter=0.0)
    assert result == 5.0 * 0.5


def test_delay_seconds_jitter_scales_result() -> None:
    policy = BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=1000.0)
    assert delay_seconds(1, policy, jitter=0.5) == 1.0 * 1.0
