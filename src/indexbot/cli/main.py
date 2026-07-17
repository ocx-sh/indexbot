"""`indexbot` CLI entrypoint — argparse subparsers wired to production
adapters (ADR-4 BD-1; WP2-M).

`_DISPATCH` is seeded from `cli/_wiring.py`'s `DISPATCH` — the one module
that constructs real `adapters/*` instances — so this file itself never
imports `adapters/*` or `httpx`. `_ARG_POPULATORS` supplies each registered
subcommand's CLI surface: `validate`, `classify-pr`, and `governance-check`
reuse their own modules' `add_arguments` (that convention); `announce`,
`reconcile`, `render`, and `seed-import` don't define an equivalent
`add_arguments` of their own (CONTRACTS.md §12 documents each module's
expected `args.*` attributes only in prose), so this file hand-rolls their
argparse surfaces directly from those docstrings. See `open_questions` for
the resulting convention gap.

Exit-code contract: argparse's own convention (missing/unknown subcommand,
`--version`/`--help`) exits 2/0 unchanged, per argparse convention. A
dispatched handler that raises an `IndexBotError` exits with that error's
mapped code (ADR-4 BD-2) — this is the single place that mapping happens;
anything else propagates as an unhandled traceback — this file never
swallows a bug.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import cast

from indexbot import __version__
from indexbot.cli import classify_pr as _classify_pr_cli
from indexbot.cli import governance_check as _governance_check_cli
from indexbot.cli import validate as _validate_cli
from indexbot.cli._wiring import DISPATCH as _PRODUCTION_DISPATCH
from indexbot.errors import IndexBotError
from indexbot.exit_codes import ExitCode

_DISPATCH: dict[str, Callable[[argparse.Namespace], ExitCode]] = dict(_PRODUCTION_DISPATCH)
"""Subcommand name -> handler, seeded from `cli/_wiring.DISPATCH` (WP2-M):
`announce`, `reconcile`, `validate`, `render`, `seed-import`. A plain `dict`
copy (not a re-exported reference) so tests may freely `monkeypatch.setitem`
this module's own `_DISPATCH` without mutating `cli/_wiring.DISPATCH`."""


def _add_announce_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--package",
        default=None,
        help=(
            "<namespace>/<package> override for manual/local invocation — bypasses the "
            "PACKAGE_ID env var entirely; the real announce.yml workflow never passes this"
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="stop after payload-shape validation only (no network, no write scope)",
    )


def _add_reconcile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run", action="store_true", help="report drift without committing or opening a PR"
    )
    parser.add_argument(
        "--package", default=None, help="scope the sweep to one <namespace>/<package>"
    )


def _add_render_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--index-dir", required=True, help="p/ listing prefix within the checkout")
    parser.add_argument(
        "--site-dist", default=None, help="write wrapper_pages under this prefix (pre-build)"
    )
    parser.add_argument(
        "--out", default=None, help="write dist_files under this prefix (post-build)"
    )
    parser.add_argument(
        "--check", action="store_true", help="report drift against the given tree(s), write nothing"
    )


def _add_seed_import_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog-md", required=True, help="local CATALOG.md seed file")
    parser.add_argument("--mirror-yml", required=True, help="local mirror.yml seed file")
    parser.add_argument("--logo", default=None, help="optional local .svg/.png logo file")
    parser.add_argument(
        "--namespace",
        default=None,
        help="explicit namespace (derived from --catalog-md if omitted)",
    )
    parser.add_argument(
        "--package",
        default=None,
        help="explicit package (derived from --catalog-md if omitted)",
    )
    parser.add_argument("--out", default=None, help='output root prefix, defaults to "p"')
    parser.add_argument("--owner-github", required=True, help="initial owner's GitHub login")
    parser.add_argument("--owner-github-id", required=True, help="initial owner's stable GitHub id")
    parser.add_argument("--upstream-org", default=None)
    parser.add_argument("--upstream-repository-url", default=None)
    parser.add_argument("--upstream-disclaimer", default=None)


_ARG_POPULATORS: dict[str, Callable[[argparse.ArgumentParser], None]] = {
    "announce": _add_announce_arguments,
    "reconcile": _add_reconcile_arguments,
    "validate": _validate_cli.add_arguments,
    "render": _add_render_arguments,
    "seed-import": _add_seed_import_arguments,
    "classify-pr": _classify_pr_cli.add_arguments,
    "governance-check": _governance_check_cli.add_arguments,
}
"""Subcommand name -> its subparser's CLI-surface populator. A name present
in `_DISPATCH` but absent here (e.g. a test's `monkeypatch`-injected handler)
gets a bare, zero-argument subparser — unchanged from the Phase 1 scaffold's
behavior."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="indexbot")
    parser.add_argument("--version", action="version", version=f"indexbot {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in _DISPATCH:
        subparser = subparsers.add_parser(name)
        populate = _ARG_POPULATORS.get(name)
        if populate is not None:
            populate(subparser)
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
