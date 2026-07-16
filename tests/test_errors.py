from __future__ import annotations

import pytest

from indexbot.errors import AnomalyError, IndexBotError, TransientError, ValidationError
from indexbot.exit_codes import ExitCode


def test_validation_error_exit_code_and_message() -> None:
    err = ValidationError("bad input")
    assert err.exit_code == ExitCode.VALIDATION_FAILURE
    assert str(err) == "bad input"


def test_anomaly_error_exit_code() -> None:
    assert AnomalyError("digest drift on an observed tag").exit_code == ExitCode.ANOMALY


def test_transient_error_exit_code() -> None:
    assert TransientError("backoff exhausted").exit_code == ExitCode.TRANSIENT


def test_base_error_default_exit_code() -> None:
    assert IndexBotError("generic").exit_code == ExitCode.VALIDATION_FAILURE


def test_errors_are_raisable_and_catchable_as_base() -> None:
    with pytest.raises(IndexBotError):
        raise ValidationError("boom")
