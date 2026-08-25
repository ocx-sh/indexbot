"""`indexbot` CLI entrypoint — argparse subparsers wired to production
adapters (ADR-4 BD-1; WP2-M).

`_DISPATCH` is seeded from `cli/_wiring.py`'s `DISPATCH` — the one module
that constructs real `adapters/*` instances — so this file itself never
imports `adapters/*` or `httpx`. `_ARG_POPULATORS` supplies each registered
subcommand's CLI surface: `validate`, `classify-pr`, `governance-check`,
`announce`, and `reconcile` reuse their own modules' `add_arguments` (fork-PR
announce revamp widened that convention to cover both — their CLI surfaces
are non-trivial enough, mutually-exclusive groups included, to live next to
the module they belong to); `render` and `seed-import` don't define an
equivalent `add_arguments` of their own (CONTRACTS.md §12 documents each
module's expected `args.*` attributes only in prose), so this file
hand-rolls their argparse surfaces directly from those docstrings. See
`open_questions` for the resulting convention gap.

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

from ocx_indexbot import __version__
from ocx_indexbot.cli import announce as _announce_cli
from ocx_indexbot.cli import ci_cmd as _ci_cli
from ocx_indexbot.cli import classify_pr as _classify_pr_cli
from ocx_indexbot.cli import governance_check as _governance_check_cli
from ocx_indexbot.cli import governance_gate as _governance_gate_cli
from ocx_indexbot.cli import governance_poll as _governance_poll_cli
from ocx_indexbot.cli import label_failed_run as _label_failed_run_cli
from ocx_indexbot.cli import reconcile as _reconcile_cli
from ocx_indexbot.cli import schema_cmd as _schema_cli
from ocx_indexbot.cli import stale as _stale_cli
from ocx_indexbot.cli import validate as _validate_cli
from ocx_indexbot.cli import validate_pr as _validate_pr_cli
from ocx_indexbot.cli import workflows_check as _workflows_check_cli
from ocx_indexbot.cli._common import write_ci_summary
from ocx_indexbot.cli._wiring import DISPATCH as _PRODUCTION_DISPATCH
from ocx_indexbot.errors import IndexBotError
from ocx_indexbot.exit_codes import ExitCode

_DISPATCH: dict[str, Callable[[argparse.Namespace], ExitCode]] = dict(_PRODUCTION_DISPATCH)
"""Subcommand name -> handler, seeded from `cli/_wiring.DISPATCH` (WP2-M):
`announce`, `reconcile`, `validate`, `render`, `seed-import`. A plain `dict`
copy (not a re-exported reference) so tests may freely `monkeypatch.setitem`
this module's own `_DISPATCH` without mutating `cli/_wiring.DISPATCH`."""


def _add_render_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--index-dir", required=True, help="p/ listing prefix within the checkout")
    parser.add_argument(
        "--out", required=True, help="write the rendered dist tree under this prefix"
    )
    parser.add_argument(
        "--check", action="store_true", help="report drift against the --out tree, write nothing"
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="render an index with no package roots (a new index before its first announce)",
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
    parser.add_argument(
        "--repository",
        default=None,
        help=(
            "override physical oci://<host>/<path> repository (validated against the host "
            "allowlist + OCI repository grammar); wins over mirror.yml — the post-M-1 escape "
            "hatch for a package whose mirror.yml still names a non-allowlisted registry"
        ),
    )
    parser.add_argument(
        "--allow-reserved-namespace",
        action="store_true",
        help=(
            "admit OCX's own brand namespace segments (ocx, ocx-sh, ocx-contrib, ocx-rs) only "
            "— control-path and generic reserved segments (p, admin, ...) stay blocked"
        ),
    )


_ARG_POPULATORS: dict[str, Callable[[argparse.ArgumentParser], None]] = {
    "announce": _announce_cli.add_arguments,
    "reconcile": _reconcile_cli.add_arguments,
    "validate": _validate_cli.add_arguments,
    "validate-pr": _validate_pr_cli.add_arguments,
    "render": _add_render_arguments,
    "seed-import": _add_seed_import_arguments,
    "classify-pr": _classify_pr_cli.add_arguments,
    "governance-check": _governance_check_cli.add_arguments,
    "governance-gate": _governance_gate_cli.add_arguments,
    "governance-poll": _governance_poll_cli.add_arguments,
    "label-failed-run": _label_failed_run_cli.add_arguments,
    "stale": _stale_cli.add_arguments,
    "ci": _ci_cli.add_arguments,
    "workflows-check": _workflows_check_cli.add_arguments,
    "schema": _schema_cli.add_arguments,
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
        # Observability floor (register §5, BCR #176): every publisher-visible
        # failure surfaces a structured reason on the workflow run's job
        # summary in addition to its stderr line — the single chokepoint, so
        # no subcommand can regress to a bare error exit. The BD-2 exit-code
        # mapping (`exc.exit_code`) is unchanged — this only ADDS the emit.
        #
        # `str(exc)` is NOT caller-owned text: a `ForgeError`'s message
        # embeds up to 400 bytes of the remote forge's own response body
        # (`adapters/_http.raise_for_status`), and this chokepoint catches
        # every `IndexBotError` subclass alike, so it cannot tell which
        # messages are safe. `write_ci_summary`'s `reason` position renders
        # unfenced — exactly the ADR-4 BD-4 spot reserved for text this
        # process authored itself (`cli/validate_pr.py` follows the same
        # split) — so the exception text goes in `detail`, fenced, and
        # `reason` stays a fixed string this code wrote.
        print(str(exc), file=sys.stderr)
        write_ci_summary(
            f"indexbot {command} failed",
            f"exit {int(exc.exit_code)}. Details below.",
            str(exc),
        )
        return int(exc.exit_code)


if __name__ == "__main__":
    sys.exit(main())
