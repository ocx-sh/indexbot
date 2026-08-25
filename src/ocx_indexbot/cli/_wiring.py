"""Production dependency-injection wiring (WP2-M) — the ONLY module that
constructs real `adapters/*` instances (ADR-4 BD-1, functional core /
imperative shell). `cli/main.py` seeds its `_DISPATCH` table from `DISPATCH`
below; nothing else under `cli/` ever imports `adapters/*` directly.

Each `_run_*` function builds its own port set at *call* time, not at import
time. This matters because several `indexbot` subcommands run in CI jobs that
deliberately hold no write-scoped credential at all — `validate.yml`'s
`schema-validate-pr` job runs `indexbot validate` with "no network, no write
scope" (no `GITHUB_TOKEN`/`GITHUB_REPOSITORY` in its env). If `DISPATCH`'s
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
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from ocx_indexbot.adapters.github_api import GITHUB_API_URL, GITHUB_GRAPHQL_URL, GitHubApi
from ocx_indexbot.adapters.gitlab_api import GITLAB_API_URL, GitLabApi
from ocx_indexbot.adapters.local_files import LocalFiles
from ocx_indexbot.adapters.local_git import LocalGit
from ocx_indexbot.adapters.registry_v2 import (
    BUILTIN_REGISTRIES,
    RegistryV2,
    RoutedRegistry,
)
from ocx_indexbot.adapters.system_clock import SystemClock
from ocx_indexbot.cli import (
    ci_cmd,
    classify_pr,
    governance_check,
    governance_gate,
    governance_poll,
    label_failed_run,
    reconcile,
    render,
    schema_cmd,
    seed_import,
    stale,
    validate,
    validate_pr,
    workflows_check,
)
from ocx_indexbot.core.policy import (
    INDEX_POLICY_PATH,
    Forge,
    IndexPolicy,
    RegistryConfig,
    parse_index_policy,
)
from ocx_indexbot.errors import ValidationError

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Mapping

    from ocx_indexbot.exit_codes import ExitCode
    from ocx_indexbot.ports import FilePort, ForgePort


def _require_env(name: str) -> str:
    """Read a required, trusted (GitHub-Actions-runner-set, not
    attacker-controlled) environment variable.

    Distinct from `cli/_common.read_validated_env`, which is reserved for
    shape-validating untrusted `repository_dispatch` payload fields (ADR-4
    BD-4) — every variable read through here (`GITHUB_TOKEN`,
    `GITHUB_REPOSITORY`, `CI_PROJECT_ID`, `GITLAB_TOKEN`) is set by the
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


def _registry_client(config: RegistryConfig, environ: Mapping[str, str]) -> RegistryV2:
    """One `RegistryV2` for one allowlisted host, from that host's own
    committed entry.

    A bare-string entry states nothing, so it takes the built-in table's
    facts where there are any (`adapters/registry_v2.BUILTIN_REGISTRIES` —
    Artifactory's token path for `ocx.sh`, GitLab's fixed `service`) and the
    Registry v2 conventions otherwise. An entry that states its own
    `base_url` is explicit about where it lives, so the built-in table is not
    consulted for it at all: inheriting `ocx.sh`'s token path into a policy
    that repointed `ocx.sh` at an internal mirror would send credentials to
    an address nobody wrote down.

    The credential is read here, from the process environment, by the name
    the entry gives — never from the file, which is committed. A named
    variable that is unset yields an anonymous client; whether that is a
    degradation or a failure is the lane's decision, not this function's
    (`_require_registry_credentials` below).
    """
    empty: Mapping[str, str] = {}
    defaults = empty if config.base_url else BUILTIN_REGISTRIES.get(config.host, empty)
    return RegistryV2(
        base_url=config.base_url or f"https://{config.host}",
        host=config.host,
        realm=config.realm or defaults.get("realm", ""),
        service=config.service or defaults.get("service", ""),
        auth=config.auth,
        credentials=environ.get(config.credentials_env, "") if config.credentials_env else "",
    )


def _registry(policy: IndexPolicy, *, credentialed: bool) -> RoutedRegistry:
    """One `RegistryV2` per allowlisted host, dispatched per call by the
    `oci://<host>/…` repository URI.

    Per call, not per run: `validate` is handed whatever roots a PR changed
    and `reconcile` walks every root in the index, so a single run routinely
    spans more than one host.

    Built from `policy.registries`, so every host this index admits has a
    client by construction. That is what retired the old
    "allowlisted-but-unservable" refusal: a host could not be named in the
    allowlist and be unreachable by this bot, because the allowlist entry IS
    the client's configuration.

    `credentialed=False` is the fork lane, and it is a construction, not a
    condition: the clients are built from an empty environment, so no
    credential exists to be sent no matter what the policy in front of this
    process declares. That matters because `validate` reads its policy out of
    PR-head content, and an entry states both the address a credential is
    sent to (`base_url`) and the variable it is read from — a pair a pull
    request must never get to choose. The privileged lanes read a policy
    nobody but a reviewer wrote, and pass `True`.
    """
    environ: Mapping[str, str] = os.environ if credentialed else {}
    return RoutedRegistry(
        {host: _registry_client(config, environ) for host, config in policy.registries.items()}
    )


def _anonymous_registry(policy: IndexPolicy) -> RoutedRegistry:
    """`_registry(..., credentialed=False)` as a one-argument factory, for
    `validate-pr` — which builds its registry from the *base-ref* policy it
    reads mid-run, not from the one this module can see."""
    return _registry(policy, credentialed=False)


def _require_registry_credentials(policy: IndexPolicy) -> None:
    """Refuse to start a privileged lane that is missing a credential its own
    policy declares.

    `reconcile` is the lane that carries registry truth for every root,
    including the ones the unprivileged fork lane could only check the shape
    of (a fork's pipeline holds no secret, by design). If this lane ran with
    a silently anonymous client, those roots would be verified by nobody at
    all, and the whole degrade-then-cover arrangement would quietly become
    degrade-then-forget. Missing credentials fail the job instead, naming
    every variable that is unset.
    """
    missing = sorted(
        config.credentials_env
        for config in policy.registries.values()
        if config.credentials_env and not os.environ.get(config.credentials_env)
    )
    if missing:
        raise ValidationError(
            f"registry credentials are not set: {missing} — {INDEX_POLICY_PATH} declares them "
            "for this index's registries, and this lane is what verifies registry truth for "
            "roots the fork lane can only check the shape of. Set them as repository secrets "
            "(GitHub, rendered into the job by `indexbot ci`) or masked, protected CI/CD "
            "variables (GitLab)."
        )


def _index_policy(raw: bytes | None) -> IndexPolicy:
    """This deployment's committed configuration, from
    `.github/index-policy.json` — its `name`, `name_segments`, G-03 allowlist,
    reserved namespaces, merge policy and forge.

    `raw` is the policy file's bytes as read through whichever port the
    calling subcommand already holds (`FilePort` for the checkout-resident
    subcommands, `ForgePort` at the base ref for the privileged ones that
    never check the repository out — both return `None` when the path does
    not exist).

    One failure mode, raised here at wiring time before the subcommand does
    any work: **no policy file**. Fail closed rather than fall back to a
    compiled-in default — an index copy that never stated a policy must say
    so out loud, not silently inherit the public index's `ghcr.io`.

    The second failure this used to raise — "a host no registry adapter can
    serve" — no longer exists to raise. It guarded a real gap while the
    servable hosts were a compiled-in triple: allowlisting
    `harbor.corp.internal` produced roots that validated and then failed
    every byte fetch. Now the allowlist entry *is* the client's
    configuration (`_registry` above), so a host cannot be admitted and
    unreachable at the same time.
    """
    if raw is None:
        raise ValidationError(
            f"{INDEX_POLICY_PATH} not found — this index has no committed policy. "
            "Every index copy commits its own (a reviewed file, never an environment "
            'variable): {"name": "acme.corp", "name_segments": 2, '
            '"registry_hosts": ["ghcr.io"]}'
        )
    return parse_index_policy(raw)


def _local_policy(files: FilePort) -> IndexPolicy:
    """`_index_policy` over the checked-out repository (`validate`,
    `reconcile`, `seed-import` all run inside an index checkout)."""
    return _index_policy(files.read_bytes(INDEX_POLICY_PATH))


def _forge_kind(args: argparse.Namespace | None = None) -> Forge:
    """Which forge API to talk to, from the runner's own variables.

    Not from `.github/index-policy.json`'s `ci.forge`, for a reason that is
    not a preference: the privileged subcommands read that policy **through
    the port this decides**, so a policy-derived choice would need the port it
    is supposed to be choosing. The runner's variable is also the more
    accurate fact — `ci.forge` says where the index is hosted, which is what
    `indexbot ci` renders workflows for, and says nothing about where a given
    process is running.

    `workflows-check` is the one subcommand a human runs outside any runner,
    so it carries an explicit `--forge`. The fallback stays GitHub: that is a
    transport default for a flag, unlike the `ocx.sh` prefix 0.2.0 removed,
    which was baked into published bytes.
    """
    explicit = None if args is None else cast("str | None", getattr(args, "forge", None))
    if explicit is not None:
        return cast("Forge", explicit)
    return "gitlab" if os.environ.get("GITLAB_CI") else "github"


def _project_api(project: str, *, kind: Forge, token: str) -> ForgePort:
    """One forge client for `project`.

    `project` is whatever that forge calls a repository: `<owner>/<repo>` on
    GitHub, a numeric id or a full namespace path on GitLab. `token` may be
    empty — a public project's read endpoints work unauthenticated.
    """
    if kind == "gitlab":
        return GitLabApi(
            project=project,
            token=token,
            base_url=os.environ.get("CI_API_V4_URL", GITLAB_API_URL),
        )
    owner, _, repo = project.partition("/")
    return GitHubApi(
        owner=owner,
        repo=repo,
        token=token,
        base_url=os.environ.get("GITHUB_API_URL", GITHUB_API_URL),
        graphql_url=os.environ.get("GITHUB_GRAPHQL_URL", GITHUB_GRAPHQL_URL),
    )


def _forge_api() -> ForgePort:
    """The repository this process is running against, from the runner's env.

    GitLab needs `$CI_PROJECT_ID` — the numeric id every job gets for free —
    and `$GITLAB_TOKEN`, which is **not** free: `$CI_JOB_TOKEN` cannot write
    labels, notes or merge requests, so a GitLab deployment sets a project or
    group access token as a masked CI variable. `$CI_API_V4_URL` is what makes
    a self-hosted instance work with no further configuration; it is the only
    one of the three with a default, because gitlab.com is the only instance
    whose address is knowable in advance.

    On GitHub, `$GITHUB_REPOSITORY` and `$GITHUB_TOKEN`, both runner-set. A
    missing variable raises by name, which is also the right message for "not
    on a supported runner".
    """
    if _forge_kind() == "gitlab":
        return _project_api(
            _require_env("CI_PROJECT_ID"), kind="gitlab", token=_require_env("GITLAB_TOKEN")
        )
    return _project_api(
        _require_env("GITHUB_REPOSITORY"), kind="github", token=_require_env("GITHUB_TOKEN")
    )


def _run_reconcile(args: argparse.Namespace) -> ExitCode:
    files = LocalFiles(root=_repo_root())
    policy = _local_policy(files)
    # The privileged lane: it must hold every credential its policy declares,
    # because it is what covers the roots the fork lane could not verify.
    _require_registry_credentials(policy)
    return reconcile.run(
        args,
        files=files,
        registry=_registry(policy, credentialed=True),
        github=_forge_api(),
        policy=policy,
    )


def _run_validate(args: argparse.Namespace) -> ExitCode:
    files = LocalFiles(root=_repo_root())
    # `--base-dir` is optional: absent, `validate` sees no base-ref bytes and
    # treats every reserved-segment root as a fresh claim (fail-closed).
    base_dir = cast("str | None", args.base_dir)
    policy = _local_policy(files)
    return validate.run(
        args,
        files=files,
        registry=_registry(policy, credentialed=False),
        policy=policy,
        base_files=LocalFiles(root=Path(base_dir)) if base_dir else None,
    )


def _run_validate_pr(args: argparse.Namespace) -> ExitCode:
    """`validate` plus the three shell steps that used to surround it.

    The base-ref tree is a fresh `tempfile.mkdtemp()`, which is `$TMPDIR` and
    therefore **outside the checkout** — deliberately, and it is the one
    binding decision this function makes rather than passes through. The
    PR-head tree is what `validate` byte-compares against its own canonical
    serialization, so writing base-ref copies into it would make every
    changed root fail the byte-exact check. It is not cleaned up: the process
    exits moments later, in a job whose runner is discarded, and an
    `atexit`/`finally` teardown would be a second failure path guarding
    nothing.

    No `policy=` is passed, unlike every other subcommand here. This job
    checks out PR-head content, so `_local_policy(files)` would read the
    pull request's own `.github/index-policy.json` — the file that decides
    which paths this gate validates. `validate_pr` resolves it from the base
    ref itself, through the `GitPort` above, because only it knows the base
    commit.
    """
    return validate_pr.run(
        args,
        git=LocalGit(repo=_repo_root()),
        files=LocalFiles(root=_repo_root()),
        registry_for=_anonymous_registry,
        base_files=LocalFiles(root=Path(tempfile.mkdtemp(prefix="indexbot-base-"))),
    )


def _run_workflows_check(args: argparse.Namespace) -> ExitCode:
    return workflows_check.run(args, files=LocalFiles(root=_repo_root()))


def _run_ci(args: argparse.Namespace) -> ExitCode:
    files = LocalFiles(root=_repo_root())
    return ci_cmd.run(args, files=files, policy=_local_policy(files))


def _run_render(args: argparse.Namespace) -> ExitCode:
    files = LocalFiles(root=_repo_root())
    return render.run(args, files=files, policy=_local_policy(files))


def _run_seed_import(args: argparse.Namespace) -> ExitCode:
    files = LocalFiles(root=_repo_root())
    policy = _local_policy(files)
    _require_registry_credentials(policy)
    return seed_import.run(
        args,
        registry=_registry(policy, credentialed=True),
        files=files,
        clock=SystemClock(),
        policy=policy,
    )


_BASE_REF_ENV: Final[tuple[str, ...]] = (
    "INDEXBOT_BASE_REF",
    "GITHUB_BASE_REF",
    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
)
"""Env vars naming the branch a pull request targets, most explicit first.

`INDEXBOT_BASE_REF` is the forge-independent escape hatch. The other two are
what GitHub Actions and GitLab CI set on a pull- or merge-request event. Every
one of them is *parent*-controlled — a fork can push to no branch of the index
— so reading a policy at any of them is the same trust direction as reading it
at the default branch, and it has the property `_DEFAULT_BASE_REF` does not:
it agrees with the ref `cli/validate_pr.py` diffs against, so both halves of
the gate judge one pull request under one policy even when it targets a branch
that is not called `main`.

The one thing this hands an author is the *choice* of which parent branch's
policy applies, by choosing what to target. That choice is bounded by what the
operator has published: every candidate is a branch only the parent can write,
and a merge into one of them publishes nothing — the deploy lane runs on the
default branch, and reaching it takes a second pull request judged under its
policy. An operator keeping a branch with a weaker policy around is the whole
of the exposure, and the previous behaviour hid it rather than removing it:
the diff already came from the pull request's base while the policy came from
`main`, so the two halves disagreed on which tree they were judging.
"""


_DEFAULT_BASE_REF: Final[str] = "main"
"""`_base_ref`'s last fallback — what the privileged subcommands read this
deployment's policy file at when the runner names no target branch, which is
every scheduled lane. `.github/index-policy.json` names an owner and a forge
but never a default-branch name (GitLab and a corporate GitHub org alike are
free to call it something else), so this is a floor, not a policy field."""


def _base_ref(environ: Mapping[str, str]) -> str:
    """The branch the running pull request targets, or `_DEFAULT_BASE_REF`.

    A deployment whose default branch is not `main` — GitLab's `master`
    holdovers, a corporate GitHub org's `trunk` — has no policy field naming
    it: `.github/index-policy.json` names an owner and a forge and stops
    there. So it comes from the runner, which knows.
    """
    for name in _BASE_REF_ENV:
        value = environ.get(name)
        if value:
            return value
    return _DEFAULT_BASE_REF


def _base_ref_policy(github: ForgePort) -> IndexPolicy:
    """The index's committed policy, read from the BASE ref over the API.

    The privileged `pull_request_target` subcommands never check the
    repository out, so they cannot read a local file — and must not read the
    PR head's copy even if they could: a fork could then declare its own
    `name_segments` and change how its own diff is classified (FP-7, G-16).
    """
    return _index_policy(github.get_file_contents(INDEX_POLICY_PATH, _base_ref(os.environ)))


def _run_classify_pr(args: argparse.Namespace) -> ExitCode:
    github = _forge_api()
    return classify_pr.run(args, github=github, policy=_base_ref_policy(github))


def _run_governance_check(args: argparse.Namespace) -> ExitCode:
    github = _forge_api()
    return governance_check.run(args, github=github, policy=_base_ref_policy(github))


def _run_governance_poll(args: argparse.Namespace) -> ExitCode:
    """Same port set and same base-ref policy read as `governance-check` —
    the poll lane differs in *when* it runs, never in what it may see."""
    github = _forge_api()
    return governance_poll.run(args, github=github, policy=_base_ref_policy(github))


def _run_governance_gate(args: argparse.Namespace) -> ExitCode:
    """Same port set and same base-ref policy read as `governance-check`/
    `governance-poll` — `governance-gate` folds classify + check + arm/
    withdraw into one process, never a different trust boundary.

    `--arm-only` is the exception, and deliberately so: it classifies nothing,
    so it needs no policy — and fetching one anyway would give the withdraw a
    second way to not happen (a base-ref read that 5xx's, a policy file the
    gate job already choked on). The generated GitHub lane runs that job on
    `if: ${{ !cancelled() }}` precisely so a failed gate still withdraws, so
    every avoidable failure mode is removed from its path.
    """
    if cast(bool, args.arm_only):
        return governance_gate.run_arm_only(args, github=_forge_api())
    github = _forge_api()
    return governance_gate.run(args, github=github, policy=_base_ref_policy(github))


def _run_label_failed_run(args: argparse.Namespace) -> ExitCode:
    """No policy read: FP-8 scoping is fork-vs-same-repo, a fact
    `ForgePort.find_pull_request_by_head_sha` already resolves, not a
    deployment-configurable one."""
    return label_failed_run.run(args, github=_forge_api())


def _run_stale(args: argparse.Namespace) -> ExitCode:
    """No policy read, for the same reason as `label-failed-run`: the
    thresholds/labels/messages `cli/stale.py` acts on are package constants,
    not `.github/index-policy.json` fields — see that module's docstring."""
    return stale.run(args, github=_forge_api(), clock=SystemClock())


DISPATCH: dict[str, Callable[[argparse.Namespace], ExitCode]] = {
    "reconcile": _run_reconcile,
    "validate": _run_validate,
    "validate-pr": _run_validate_pr,
    "ci": _run_ci,
    "render": _run_render,
    "seed-import": _run_seed_import,
    "classify-pr": _run_classify_pr,
    "governance-check": _run_governance_check,
    "governance-gate": _run_governance_gate,
    "governance-poll": _run_governance_poll,
    "label-failed-run": _run_label_failed_run,
    "stale": _run_stale,
    "workflows-check": _run_workflows_check,
    # No port of any kind: the schema is package data, read through
    # importlib.resources and written to stdout.
    "schema": schema_cmd.run,
}
"""Production subcommand name -> handler, matching `cli/main.py`'s
`_DISPATCH: dict[str, Callable[[argparse.Namespace], ExitCode]]` shape
exactly — no `functools.partial`/closure adaptation needed at the `main.py`
call site, since every port construction already happens inside each
`_run_*` function above.

`classify-pr`/`governance-check`/`governance-gate`/`governance-poll` all
reuse `_forge_api()`, which exposes only `GITHUB_TOKEN`/`GITHUB_REPOSITORY`
(no write-scoped `RegistryPort`/`FilePort` credential; none of these
subcommands needs one, CONTRACTS.md §12). What that token must be *allowed*
to do differs by invocation, and only that: `governance-gate` arms or
withdraws auto-merge unless `--no-arm` is given, and arming is a deferred
write to the base branch. The generated GitHub lane keeps that scope in its
own `arm-auto-merge` job (`--arm-only`) rather than granting it to the job
that classifies; GitLab's poller holds it throughout, because a schedule is
the only privileged actor that forge has.
"""
