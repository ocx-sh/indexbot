"""`indexbot schema` — print the shipped policy schema to stdout."""

from __future__ import annotations

import argparse
import json

import pytest

from ocx_indexbot.cli import schema_cmd
from ocx_indexbot.exit_codes import ExitCode


def test_writes_the_schema_to_stdout_and_exits_ok(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert schema_cmd.run(argparse.Namespace()) is ExitCode.OK
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["$id"].endswith(schema_cmd.SCHEMA_RESOURCE)


def test_the_shipped_resource_is_reachable_via_importlib() -> None:
    """Read through `importlib.resources`, not a `__file__`-relative path —
    this is the assertion that would fail if the schema stopped being packaged
    into the wheel."""
    assert json.loads(schema_cmd.read_schema())["type"] == "object"


def test_add_arguments_declares_no_surface() -> None:
    """`schema` takes no arguments; the populator exists only so
    `cli/main.py`'s table stays total over the registered subcommands."""
    parser = argparse.ArgumentParser()
    schema_cmd.add_arguments(parser)
    assert parser.parse_args([]) == argparse.Namespace()


def test_the_shipped_entrypoint_really_reaches_this_module(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Through `main()`, not `run()` directly.

    `schema` was the one DISPATCH entry no test drove through the real
    argument parser, so a broken registration — a renamed subcommand, a
    populator that stopped being called — would have type-checked, passed at
    100% coverage, and failed only for whoever ran the published command.
    """
    from ocx_indexbot.cli import main as main_module

    assert main_module.main(["schema"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["type"] == "object"
