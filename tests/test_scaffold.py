# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""Scaffold smoke tests — the packaging claims and the entrypoint contract.

Replaced piecemeal as the real subcommands land; `test_version_*` stays, since
the metadata lookup name drifting away from `[project] name` is exactly the
failure `importlib.metadata` reports as "not installed" rather than as an error.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError

import pytest

import ocx_indexbot
from ocx_indexbot.cli.main import main


def test_version_is_non_empty_semver_prefix() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", ocx_indexbot.__version__), ocx_indexbot.__version__


def test_version_exported_in_all() -> None:
    assert "__version__" in ocx_indexbot.__all__


def test_resolve_version_falls_back_when_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(ocx_indexbot, "_pkg_version", _raise)
    resolve = ocx_indexbot._resolve_version  # pyright: ignore[reportPrivateUsage]
    assert resolve() == "0.0.0+unknown"


def test_bare_invocation_is_a_failure_and_says_so_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no subcommands are wired yet" in captured.err


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert ocx_indexbot.__version__ in capsys.readouterr().out
