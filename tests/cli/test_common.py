from __future__ import annotations

from pathlib import Path

import pytest

from ocx_indexbot.cli import _common


def test_write_ci_output_single_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    _common.write_ci_output("result", "applied")

    content = output_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0].startswith("result<<")
    delimiter = lines[0].removeprefix("result<<")
    assert lines[1] == "applied"
    assert lines[2] == delimiter


def test_write_ci_output_multiline_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    _common.write_ci_output("body", "line one\nline two")

    content = output_file.read_text(encoding="utf-8")
    assert "line one\nline two" in content


def test_write_ci_output_appends_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    _common.write_ci_output("result", "no-op")
    _common.write_ci_output("pr_number", "42")

    content = output_file.read_text(encoding="utf-8")
    assert "result<<" in content
    assert "pr_number<<" in content


def test_write_ci_output_with_no_sink_at_all_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("INDEXBOT_OUTPUT", raising=False)
    with pytest.raises(RuntimeError, match="neither GITHUB_OUTPUT nor INDEXBOT_OUTPUT"):
        _common.write_ci_output("result", "applied")


# ---- the GitLab sink -------------------------------------------------------


def test_write_ci_output_writes_a_gitlab_dotenv_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dotenv` reports become CI variables verbatim, so the name is
    upper-cased and the value quoted — the shape GitLab's parser reads."""
    output_file = tmp_path / "indexbot.env"
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("INDEXBOT_OUTPUT", str(output_file))

    _common.write_ci_output("classification", "new-package")

    assert output_file.read_text(encoding="utf-8") == 'CLASSIFICATION="new-package"\n'


def test_gitlab_dotenv_appends_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "indexbot.env"
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("INDEXBOT_OUTPUT", str(output_file))

    _common.write_ci_output("classification", "new-package")
    _common.write_ci_output("disposition", "success")

    assert output_file.read_text(encoding="utf-8").splitlines() == [
        'CLASSIFICATION="new-package"',
        'DISPOSITION="success"',
    ]


@pytest.mark.parametrize(
    "value",
    [
        "line one\nline two",
        'has a " quote',
        "$(id)",
        "`id`",
        "a;b",
        "a'b",
        "a\\b",
        "",
    ],
)
def test_gitlab_dotenv_refuses_a_value_it_cannot_express(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `dotenv` report is parsed line by line and has no heredoc form, and
    what it produces is a CI **variable** that downstream jobs interpolate
    into shell. So the check is an allowlist: a denylist of the two characters
    that break the file format still let `$(id)` and a backtick through. An
    unexpressible value fails here, where the message names the cause, rather
    than as an unrelated GitLab parse error two jobs later."""
    output_file = tmp_path / "indexbot.env"
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("INDEXBOT_OUTPUT", str(output_file))

    with pytest.raises(RuntimeError, match="is outside"):
        _common.write_ci_output("body", value)


def test_github_output_wins_when_both_sinks_are_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing sets both in practice; pinning the precedence keeps a stray
    leftover variable from silently redirecting a job's output."""
    actions_file = tmp_path / "output.txt"
    dotenv_file = tmp_path / "indexbot.env"
    monkeypatch.setenv("GITHUB_OUTPUT", str(actions_file))
    monkeypatch.setenv("INDEXBOT_OUTPUT", str(dotenv_file))

    _common.write_ci_output("classification", "new-package")

    assert "classification<<" in actions_file.read_text(encoding="utf-8")
    assert not dotenv_file.exists()


def test_write_ci_output_retries_on_delimiter_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    delimiters = iter(["COLLIDE", "SAFE"])
    monkeypatch.setattr(_common, "_random_delimiter", lambda: next(delimiters))

    # The value itself contains the first (colliding) delimiter candidate.
    _common.write_ci_output("body", "contains COLLIDE inside")

    content = output_file.read_text(encoding="utf-8")
    assert "body<<SAFE" in content
    assert content.count("SAFE") == 2  # opening + closing delimiter line


def test_write_ci_output_raises_when_delimiters_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(_common, "_random_delimiter", lambda: "ALWAYS_COLLIDES")

    with pytest.raises(RuntimeError, match="could not find a collision-free delimiter"):
        _common.write_ci_output("body", "contains ALWAYS_COLLIDES always")


def test_write_ci_summary_appends_markdown_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    _common.write_ci_summary("indexbot validate failed", "bad package id")

    assert summary_file.read_text(encoding="utf-8") == (
        "## indexbot validate failed\n\nbad package id\n"
    )


def test_write_ci_summary_appends_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    _common.write_ci_summary("first heading", "one")
    _common.write_ci_summary("second heading", "two")

    assert summary_file.read_text(encoding="utf-8") == (
        "## first heading\n\none\n## second heading\n\ntwo\n"
    )


def test_write_ci_summary_unset_env_writes_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    _common.write_ci_summary("indexbot reconcile failed", "anomaly detected")

    captured = capsys.readouterr()
    assert "indexbot reconcile failed" in captured.err
    assert "anomaly detected" in captured.err
    assert captured.err.count("\n") == 1  # exactly one line, nothing on stdout
    assert captured.out == ""


def test_write_ci_summary_empty_env_writes_stderr_not_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An empty (set-but-blank) value is treated identically to unset — the
    # `if not summary_path` guard covers both, so a blank path never becomes a
    # spurious file write at the filesystem root.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")

    _common.write_ci_summary("indexbot render failed", "drift detected")

    assert "indexbot render failed" in capsys.readouterr().err


def test_write_ci_summary_fences_untrusted_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`detail` is where PR-derived text goes, and it goes inside a code
    fence — the job summary is rendered markdown, so unfenced content is
    markup a pull request author chose (ADR-4 BD-4)."""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    _common.write_ci_summary("failed", "one root rejected", "p/ns/pkg.json: <img onerror=x>")

    assert summary_file.read_text(encoding="utf-8") == (
        "## failed\n\none root rejected\n\n```\np/ns/pkg.json: <img onerror=x>\n```\n"
    )


def test_write_ci_summary_fence_outgrows_backticks_in_the_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape this guards: CommonMark closes a fenced block on the first
    run of at least as many backticks as opened it, so a fixed three-backtick
    fence lets detail containing ``` break back out into rendered markdown."""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    _common.write_ci_summary("failed", "why", "a\n````\n# not a heading")

    body = summary_file.read_text(encoding="utf-8")
    assert "\n`````\na\n````\n# not a heading\n`````\n" in body


def test_write_ci_summary_detail_falls_back_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """GitLab has no job-summary surface, and the job log is the page a
    publisher opens from a failed pipeline — so the detail must still reach
    it, not vanish with the sink."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    _common.write_ci_summary("failed", "why", "p/ns/pkg.json: rejected")

    assert capsys.readouterr().err == "failed: why\np/ns/pkg.json: rejected\n"


def test_write_ci_annotation_is_a_workflow_command_on_github_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`$GITHUB_ACTIONS` is the runner's own fact, and the workflow-command
    form goes to stdout, where the generated workflow's `echo` put it."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    _common.write_ci_annotation("error", "indexbot validate-pr failed", "exit 1")

    assert capsys.readouterr().out == "::error title=indexbot validate-pr failed::exit 1\n"


def test_write_ci_annotation_is_a_plain_stderr_line_elsewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A GitLab job or a laptop has no parser for `::notice`, so it gets a
    readable line instead of a literal workflow command."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    _common.write_ci_annotation("notice", "indexbot validate-pr", "nothing to validate")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "notice: indexbot validate-pr: nothing to validate\n"
