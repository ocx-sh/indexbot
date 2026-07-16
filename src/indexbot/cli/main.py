"""`indexbot` CLI entrypoint — argparse subparsers scaffold (ADR-4 BD-1).

No subcommands are registered yet: `announce | reconcile | validate | render
| seed-import | classify-pr | governance-check` land in Phase 2. `_DISPATCH`
is already the single place a subcommand gets wired in — Phase 2 adds a
`cli/<name>.py` module and one `_DISPATCH["name"] = handler` line; nothing
else in this file changes.

Exit-code contract: argparse's own convention (missing/unknown subcommand,
`--version`) exits 2/0 unchanged, per argparse convention. A dispatched
handler that raises an `IndexBotError` exits with that error's mapped code
(ADR-4 BD-2); anything else propagates as an unhandled traceback — this file
never swallows a bug.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import cast

from indexbot import __version__
from indexbot.errors import IndexBotError
from indexbot.exit_codes import ExitCode

_DISPATCH: dict[str, Callable[[argparse.Namespace], ExitCode]] = {}
"""Subcommand name -> handler. Empty until Phase 2 registers the first one."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="indexbot")
    parser.add_argument("--version", action="version", version=f"indexbot {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in _DISPATCH:
        subparsers.add_parser(name)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, dispatch to the matching subcommand, return its exit code.

    `required=True` on the subparsers means `parser.parse_args` itself exits
    (code 2) before returning if `command` is missing or not among the
    registered subcommands — the `cast` below documents that guarantee for
    the type checker rather than re-checking it at runtime.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = cast(str, args.command)
    handler = _DISPATCH[command]
    try:
        return int(handler(args))
    except IndexBotError as exc:
        print(str(exc), file=sys.stderr)
        return int(exc.exit_code)


if __name__ == "__main__":
    sys.exit(main())
