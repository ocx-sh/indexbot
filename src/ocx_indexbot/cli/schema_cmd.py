"""`indexbot schema` — print the JSON Schema for `.github/index-policy.json`.

The schema ships inside the wheel rather than only on the docs site so an
operator can pin it: `indexbot schema > .github/index-policy.schema.json`
gives them the grammar of the *exact bot version their CI runs*, which is the
same argument that keeps the bot version itself pinned. The docs-site copy
exists for `$schema` autocomplete, where a URL is the only thing an editor
will follow.

Nothing validates against this at runtime — `core/policy.py`'s
`parse_index_policy` is the authority, because ADR-4 BD-1 keeps `httpx` the
only runtime dependency and a schema validator is not one. The two are held
together by `tests/fixtures/policy/`, a corpus every case of which must be
accepted (or rejected) by both.
"""

from __future__ import annotations

import sys
from importlib import resources
from typing import TYPE_CHECKING, Final

from ocx_indexbot.exit_codes import ExitCode

if TYPE_CHECKING:
    import argparse

SCHEMA_RESOURCE: Final[str] = "index-policy-v1.schema.json"
"""Filename within the `ocx_indexbot.schema` package directory. Read through
`importlib.resources`, never a path relative to `__file__` — the latter
breaks in a zipped or relocated install."""


def read_schema() -> str:
    """The shipped schema's bytes, decoded. Shared with the test suite, which
    validates the fixture corpus against this exact text rather than a copy."""
    return (resources.files("ocx_indexbot.schema") / SCHEMA_RESOURCE).read_text(encoding="utf-8")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """No arguments. Declared anyway so `cli/main.py`'s `_ARG_POPULATORS`
    table stays total over the registered subcommands rather than growing an
    exception for this one."""
    del parser


def run(args: argparse.Namespace) -> ExitCode:
    """Write the schema to stdout and exit `OK`.

    Deliberately stdout and not a file: the caller decides where it lands, and
    a subcommand that writes into a checkout would need a `FilePort`, a path
    argument, and an overwrite policy to do something a shell redirect already
    does.
    """
    del args
    sys.stdout.write(read_schema())
    return ExitCode.OK
