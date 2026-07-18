from __future__ import annotations

from pathlib import Path

import pytest

from indexbot.cli import _common


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
