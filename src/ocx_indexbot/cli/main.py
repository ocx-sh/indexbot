# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""`indexbot` entrypoint.

Scaffold state: the toolchain, gates and packaging are wired; the subcommands
(`announce`, `reconcile`, `validate`, `render`, `seed-import`, `classify-pr`,
`governance-check`, `workflows`) arrive with the extraction move. Diagnostics
go to stderr — stdout stays reserved for a subcommand's result.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ocx_indexbot import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="indexbot",
        description="Write path for an OCX package index.",
    )
    parser.add_argument("--version", action="version", version=f"indexbot {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help(sys.stderr)
    print("indexbot: no subcommands are wired yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
