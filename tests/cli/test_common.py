from __future__ import annotations

import re
from pathlib import Path

import pytest

from indexbot.cli import _common
from indexbot.errors import ValidationError

_LOWER_ALPHA = re.compile(r"[a-z]+")


def test_read_validated_env_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_ID", "cmake")
    value = _common.read_validated_env("PACKAGE_ID", pattern=_LOWER_ALPHA, max_length=10)
    assert value == "cmake"


def test_read_validated_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PACKAGE_ID", raising=False)
    with pytest.raises(ValidationError, match="is not set"):
        _common.read_validated_env("PACKAGE_ID", pattern=_LOWER_ALPHA, max_length=10)


def test_read_validated_env_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_ID", "")
    with pytest.raises(ValidationError, match="is not set"):
        _common.read_validated_env("PACKAGE_ID", pattern=_LOWER_ALPHA, max_length=10)


def test_read_validated_env_over_length_raises_before_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "abcdef" fullmatches _LOWER_ALPHA but exceeds max_length=3 — length is
    # checked first (ADR-4 BD-4), so this must fail on length, not pattern.
    monkeypatch.setenv("PACKAGE_ID", "abcdef")
    with pytest.raises(ValidationError, match="exceeds max length"):
        _common.read_validated_env("PACKAGE_ID", pattern=_LOWER_ALPHA, max_length=3)


def test_read_validated_env_pattern_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_ID", "CMake")
    with pytest.raises(ValidationError, match="does not match"):
        _common.read_validated_env("PACKAGE_ID", pattern=_LOWER_ALPHA, max_length=10)


def test_read_validated_env_uses_fullmatch_not_search(monkeypatch: pytest.MonkeyPatch) -> None:
    # A valid prefix followed by injected garbage must be rejected — proves
    # fullmatch (not match/search) is used.
    monkeypatch.setenv("PACKAGE_ID", "cmake; rm -rf /")
    with pytest.raises(ValidationError, match="does not match"):
        _common.read_validated_env("PACKAGE_ID", pattern=_LOWER_ALPHA, max_length=64)


def test_write_github_output_single_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    _common.write_github_output("result", "applied")

    content = output_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0].startswith("result<<")
    delimiter = lines[0].removeprefix("result<<")
    assert lines[1] == "applied"
    assert lines[2] == delimiter


def test_write_github_output_multiline_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    _common.write_github_output("body", "line one\nline two")

    content = output_file.read_text(encoding="utf-8")
    assert "line one\nline two" in content


def test_write_github_output_appends_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    _common.write_github_output("result", "no-op")
    _common.write_github_output("pr_number", "42")

    content = output_file.read_text(encoding="utf-8")
    assert "result<<" in content
    assert "pr_number<<" in content


def test_write_github_output_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_OUTPUT is not set"):
        _common.write_github_output("result", "applied")


def test_write_github_output_retries_on_delimiter_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    delimiters = iter(["COLLIDE", "SAFE"])
    monkeypatch.setattr(_common, "_random_delimiter", lambda: next(delimiters))

    # The value itself contains the first (colliding) delimiter candidate.
    _common.write_github_output("body", "contains COLLIDE inside")

    content = output_file.read_text(encoding="utf-8")
    assert "body<<SAFE" in content
    assert content.count("SAFE") == 2  # opening + closing delimiter line


def test_write_github_output_raises_when_delimiters_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(_common, "_random_delimiter", lambda: "ALWAYS_COLLIDES")

    with pytest.raises(RuntimeError, match="could not find a collision-free delimiter"):
        _common.write_github_output("body", "contains ALWAYS_COLLIDES always")
