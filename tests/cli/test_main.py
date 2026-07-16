from __future__ import annotations

import argparse
from collections.abc import Callable

import pytest

from indexbot import __version__
from indexbot.cli import main as main_module
from indexbot.errors import ValidationError
from indexbot.exit_codes import ExitCode


def _register(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    handler: Callable[[argparse.Namespace], ExitCode],
) -> None:
    """Register a subcommand for the duration of one test, auto-reverted by
    `monkeypatch`. `_DISPATCH` is intentionally private to `cli/main.py`
    (Phase 2's real subcommand modules live there too) — this is the one
    place the test suite is allowed to reach past that.
    """
    monkeypatch.setitem(main_module._DISPATCH, name, handler)  # pyright: ignore[reportPrivateUsage]


def test_version_flag_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main_module.main(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_missing_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main_module.main([])
    assert exc_info.value.code == 2


def test_unknown_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main_module.main(["bogus"])
    assert exc_info.value.code == 2


def test_dispatches_registered_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[argparse.Namespace] = []

    def handler(args: argparse.Namespace) -> ExitCode:
        calls.append(args)
        return ExitCode.OK

    _register(monkeypatch, "noop", handler)

    assert main_module.main(["noop"]) == ExitCode.OK
    assert calls[0].command == "noop"


def test_dispatch_propagates_handler_result_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_args: argparse.Namespace) -> ExitCode:
        return ExitCode.ANOMALY

    _register(monkeypatch, "noop", handler)

    assert main_module.main(["noop"]) == ExitCode.ANOMALY


def test_dispatch_translates_index_bot_error_to_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(_args: argparse.Namespace) -> ExitCode:
        raise ValidationError("bad package id")

    _register(monkeypatch, "noop", handler)

    assert main_module.main(["noop"]) == ExitCode.VALIDATION_FAILURE
    assert "bad package id" in capsys.readouterr().err
