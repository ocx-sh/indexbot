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

The same call-time rule governs this deployment's registry-host policy
(`.github/index-policy.json`, `core/policy.py`): the four subcommands that
resolve a `repository` load and check it here, at wiring time, before any
work; `render`/`classify-pr`/`governance-check` never touch a registry host
and are deliberately left able to run without a policy file at all.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from indexbot.adapters.github_api import GitHubApi
from indexbot.adapters.local_files import LocalFiles
from indexbot.adapters.registry_v2 import (
    GHCR_HOST,
    OCX_SH_HOST,
    OCX_SH_REALM,
    RegistryV2,
    RoutedRegistry,
)
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
from indexbot.core.policy import INDEX_POLICY_PATH, parse_index_policy
from indexbot.errors import ValidationError

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from indexbot.exit_codes import ExitCode
    from indexbot.ports import FilePort, GitHubPort


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


REGISTRY_ADAPTER_HOSTS: Final[frozenset[str]] = frozenset({GHCR_HOST, OCX_SH_HOST})
"""Every registry host some `RegistryPort` adapter can actually serve.

Exactly the hosts `_registry()` below wires a client for — `_registry_hosts`
refuses any deployment policy that exceeds this set. Extending it without
also wiring a client re-opens exactly the gap the guard exists to close
(roots that validate and then cannot be fetched), so the two are edited
together and `tests/cli/test_wiring.py` pins them to each other."""


def _registry() -> RoutedRegistry:
    """One `RegistryV2` per servable host, dispatched per call by the
    `oci://<host>/…` repository URI.

    Per call, not per run: `validate` is handed whatever roots a PR changed
    and `reconcile` walks every root in the index, so a single run routinely
    spans both hosts. Both are the same Registry v2 client — `ocx.sh` differs
    only in where its anonymous pull token comes from.
    """
    return RoutedRegistry(
        {
            GHCR_HOST: RegistryV2(),
            OCX_SH_HOST: RegistryV2(
                base_url=f"https://{OCX_SH_HOST}",
                host=OCX_SH_HOST,
                realm=OCX_SH_REALM,
            ),
        }
    )


def _registry_hosts(raw: bytes | None) -> frozenset[str]:
    """This deployment's G-03 allowlist, from `.github/index-policy.json`.

    `raw` is the policy file's bytes as read through whichever port the
    calling subcommand already holds (`FilePort` for the checkout-resident
    subcommands, `GitHubPort` at `main` for the publisher-side `announce` —
    both return `None` when the path does not exist).

    Two failure modes, both raised here at wiring time, before the subcommand
    does any work:

    - **No policy file.** Fail closed rather than fall back to a compiled-in
      default: an index copy that never stated a policy must say so out loud,
      not silently inherit the public index's `ghcr.io`.
    - **A host no adapter can serve** — the important one. Allowlisting
      `harbor.corp.internal` today would let a root pass every validation
      check and then fail every byte fetch, which is strictly worse than the
      honest refusal it replaces. Refused up front, naming the missing piece.
    """
    if raw is None:
        raise ValidationError(
            f"{INDEX_POLICY_PATH} not found — this index has no registry-host policy. "
            "Every index copy commits its own (a reviewed file, never an environment "
            'variable): {"registry_hosts": ["ghcr.io"]}'
        )
    hosts = parse_index_policy(raw)
    unservable = sorted(hosts - REGISTRY_ADAPTER_HOSTS)
    if unservable:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: no registry adapter can serve {unservable} — indexbot "
            f"only implements {sorted(REGISTRY_ADAPTER_HOSTS)}. Allowlisting a host with no "
            "adapter produces roots that pass validation and then cannot be fetched, so it "
            "is refused here instead: implement a RegistryPort for it in "
            "src/indexbot/adapters/, add its host to REGISTRY_ADAPTER_HOSTS, and dispatch "
            "it from cli/_wiring.py first."
        )
    return hosts


def _local_policy_hosts(files: FilePort) -> frozenset[str]:
    """`_registry_hosts` over the checked-out repository (`validate`,
    `reconcile`, `seed-import` all run inside an index checkout)."""
    return _registry_hosts(files.read_bytes(INDEX_POLICY_PATH))


def _github_api() -> GitHubApi:
    """`GITHUB_REPOSITORY` (runner-set, `<owner>/<repo>`) + `GITHUB_TOKEN`.

    Fork-PR announce revamp (ADR-6): there is no index-side write credential
    in the announce path any more (`announce.yml`/its `index-write`
    Environment/`INDEX_WRITE_TOKEN` are all retired — FP-7). The three
    privileged-job workflows that still call this (`reconcile.yml`,
    `governance.yml`'s `governance-gate` running `classify-pr`/
    `governance-check`) all expose the ambient, per-run default
    `GITHUB_TOKEN` (`github.token`) to the process under the same
    `$GITHUB_TOKEN` env var name this function reads — labels/status/
    comments/reviewer-requests/issue-filing scope only, never a repo-write
    credential.
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
    index_github = _index_github(args)
    # The one subcommand whose policy does NOT come from the local checkout:
    # a publisher runs `announce` from their own working directory (the fork
    # commit goes over the API, there is no index checkout to read), so the
    # governing policy is the target index's own committed file at `main` —
    # read through the same `GitHubPort`, at the same base ref, as the root
    # this run is about to regenerate. A publisher cannot widen it locally.
    allowed_hosts = _registry_hosts(
        index_github.get_file_contents(INDEX_POLICY_PATH, announce.BASE_REF)
    )
    return announce.run(
        args,
        registry=_registry(),
        index_github=index_github,
        fork_github=fork_github,
        files=LocalFiles(root=_repo_root()),
        clock=SystemClock(),
        allowed_hosts=allowed_hosts,
    )


def _run_reconcile(args: argparse.Namespace) -> ExitCode:
    files = LocalFiles(root=_repo_root())
    return reconcile.run(
        args,
        files=files,
        registry=_registry(),
        github=_github_api(),
        allowed_hosts=_local_policy_hosts(files),
    )


def _run_validate(args: argparse.Namespace) -> ExitCode:
    files = LocalFiles(root=_repo_root())
    return validate.run(
        args, files=files, registry=_registry(), allowed_hosts=_local_policy_hosts(files)
    )


def _run_render(args: argparse.Namespace) -> ExitCode:
    return render.run(args, files=LocalFiles(root=_repo_root()))


def _run_seed_import(args: argparse.Namespace) -> ExitCode:
    files = LocalFiles(root=_repo_root())
    return seed_import.run(
        args,
        registry=_registry(),
        files=files,
        clock=SystemClock(),
        allowed_hosts=_local_policy_hosts(files),
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
`.github/workflows/governance.yml`'s `governance-gate` job, which exposes only
`GITHUB_TOKEN`/`GITHUB_REPOSITORY` (no write-scoped `RegistryPort`/`FilePort`
credential; neither module needs one, CONTRACTS.md §12).
"""
