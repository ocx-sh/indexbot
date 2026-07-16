from __future__ import annotations

from indexbot.exit_codes import ExitCode


def test_exit_codes_match_sysexits_contract() -> None:
    assert ExitCode.OK == 0
    assert ExitCode.VALIDATION_FAILURE == 1
    assert ExitCode.ANOMALY == 65
    assert ExitCode.TRANSIENT == 75


def test_exit_code_is_int_subtype() -> None:
    assert isinstance(ExitCode.OK, int)
