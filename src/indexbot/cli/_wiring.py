"""Production dependency-injection wiring (WP2-M) — the ONLY module that
constructs real `adapters/*` instances (ADR-4 BD-1, functional core /
imperative shell). `cli/main.py` seeds its `_DISPATCH` table from `DISPATCH`
below; nothing else under `cli/` ever imports `adapters/*` directly.

Each `_run_*` function builds its own port set at *call* time, not at import
time. This matters because several `indexbot` subcommands run in CI jobs that
deliberately hold no write-scoped credential at all — `validate.yml`'s
`schema-validate-pr` job runs `indexbot validate` with "no network, no write
scope" (no `GITHUB_TOKEN`/`GITHUB_REPOSITORY` in its env), and
`cli/announce.py`'s `--out` mode reads the index repo anonymously the same
way (no `GITHUB_TOKEN` required at all, `_index_github`). If `DISPATCH`'s
values were already-constructed port instances (e.g. bound once at import
time via `functools.partial`), merely importing this module would eagerly
read `GITHUB_TOKEN` for every subcommand, including ones that need it not at
all — crashing an unprivileged job that never sets it. Deferring
construction to inside each `_run_*` function (only reached once
`cli/main.py` has already resolved which single subcommand to dispatch to)
keeps every subcommand's environment requirements independent of the others.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

from indexbot.adapters.ghcr import GhcrRegistry
from indexbot.adapters.github_api import GitHubApi
from indexbot.adapters.local_files import LocalFiles
from indexbot.adapters.system_clock import SystemClock
from indexbot.cli import (
    announce,
    classify_pr,
    governance_check,
    reconcile,
    render,
    seed_import,
    validate,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from indexbot.exit_codes import ExitCode
    from indexbot.ports import GitHubPort


def _require_env(name: str) -> str:
    """Read a required, trusted (GitHub-Actions-runner-set, not
    attacker-controlled) environment variable.

    Distinct from `cli/_common.read_validated_env`, which is reserved for
    shape-validating untrusted `repository_dispatch` payload fields (ADR-4
    BD-4) — `GITHUB_TOKEN`/`GITHUB_REPOSITORY` are set by the GitHub Actions
    runner itself, never by a PR author or webhook body, so no regex/
    length-cap discipline applies here, only presence.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value


def _repo_root() -> Path:
    """The checked-out repository root.

    `GITHUB_WORKSPACE` (set by every GitHub Actions runner, unaffected by a
    step's `working-directory:` override) if present, else the process's
    current directory for local/manual invocation.

    **Open question**: `reconcile.yml`'s "Run indexbot reconcile" step sets
    `working-directory: bot` for the shell, but this repo's `p/` tree lives
    at the checkout root, not under `bot/`. Reading `GITHUB_WORKSPACE` here
    (rather than defaulting bare to `Path(".")`) routes around that mismatch
    since `GITHUB_WORKSPACE` always points at the checkout root regardless
    of a step's shell `cwd` — confirm this is the intended fix, or whether
    `reconcile.yml` should instead drop its `working-directory: bot`
    override (out of this work package's `cli/`-only path scope to change).
    """
    return Path(os.environ.get("GITHUB_WORKSPACE", "."))


def _github_api() -> GitHubApi:
    """`GITHUB_REPOSITORY` (runner-set, `<owner>/<repo>`) + `GITHUB_TOKEN`
    (the write-scoped credential).

    All three privileged-job workflows (`announce.yml`'s `regen-and-pr`,
    `reconcile.yml`, `validate.yml`'s `governance-gate`) expose the
    `index-write` Environment's secret to the process as `$GITHUB_TOKEN` —
    the underlying GitHub secret itself is still named `INDEX_WRITE_TOKEN`
    (ADR-4 BD-6), only the `env:` key each workflow maps it to is
    standardized on `GITHUB_TOKEN` to match this function.
    """
    owner, _, repo = _require_env("GITHUB_REPOSITORY").partition("/")
    return GitHubApi(owner=owner, repo=repo, token=_require_env("GITHUB_TOKEN"))


def _index_github(args: argparse.Namespace) -> GitHubApi:
    """Read-only access to `--index-repo` at `main` — anonymous (no
    `GITHUB_TOKEN` required) works fine for a public repo's Contents API,
    matching `--out` mode's "unauthenticated read is fine" design call.
    `--fork` mode also reads through this same instance (only opening the PR
    against the index repo needs write scope here — the fork-side commit
    goes through a *separate*, always-authenticated `GitHubApi`, see
    `_run_announce`)."""
    owner, _, repo = cast(str, args.index_repo).partition("/")
    return GitHubApi(owner=owner, repo=repo, token=os.environ.get("GITHUB_TOKEN", ""))


def _run_announce(args: argparse.Namespace) -> ExitCode:
    """A local publisher tool (fork-PR announce revamp) — no index-side
    credential, no `repository_dispatch` doorbell, no privileged/unprivileged
    CI split any more. `--out` mode never touches `fork_github` (stays
    `None`); `--fork` mode needs the publisher's own write-scoped
    `GITHUB_TOKEN` to commit to their fork and open the PR."""
    fork = cast("str | None", getattr(args, "fork", None))
    fork_github: GitHubPort | None = None
    if fork:
        fork_owner, _, fork_repo = fork.partition("/")
        fork_github = GitHubApi(
            owner=fork_owner, repo=fork_repo, token=_require_env("GITHUB_TOKEN")
        )
    return announce.run(
        args,
        registry=GhcrRegistry(),
        index_github=_index_github(args),
        fork_github=fork_github,
        files=LocalFiles(root=_repo_root()),
        clock=SystemClock(),
    )


def _run_reconcile(args: argparse.Namespace) -> ExitCode:
    return reconcile.run(
        args, files=LocalFiles(root=_repo_root()), registry=GhcrRegistry(), github=_github_api()
    )


def _run_validate(args: argparse.Namespace) -> ExitCode:
    return validate.run(args, files=LocalFiles(root=_repo_root()), registry=GhcrRegistry())


def _run_render(args: argparse.Namespace) -> ExitCode:
    return render.run(args, files=LocalFiles(root=_repo_root()))


def _run_seed_import(args: argparse.Namespace) -> ExitCode:
    return seed_import.run(
        args,
        registry=GhcrRegistry(),
        files=LocalFiles(root=_repo_root()),
        clock=SystemClock(),
    )


def _run_classify_pr(args: argparse.Namespace) -> ExitCode:
    return classify_pr.run(args, github=_github_api())


def _run_governance_check(args: argparse.Namespace) -> ExitCode:
    return governance_check.run(args, github=_github_api())


DISPATCH: dict[str, Callable[[argparse.Namespace], ExitCode]] = {
    "announce": _run_announce,
    "reconcile": _run_reconcile,
    "validate": _run_validate,
    "render": _run_render,
    "seed-import": _run_seed_import,
    "classify-pr": _run_classify_pr,
    "governance-check": _run_governance_check,
}
"""Production subcommand name -> handler, matching `cli/main.py`'s
`_DISPATCH: dict[str, Callable[[argparse.Namespace], ExitCode]]` shape
exactly — no `functools.partial`/closure adaptation needed at the `main.py`
call site, since every port construction already happens inside each
`_run_*` function above.

`classify-pr`/`governance-check` both reuse `_github_api()` — matching
`.github/workflows/validate.yml`'s `governance-gate` job, which exposes only
`GITHUB_TOKEN`/`GITHUB_REPOSITORY` (no write-scoped `RegistryPort`/`FilePort`
credential; neither module needs one, CONTRACTS.md §12).
"""
