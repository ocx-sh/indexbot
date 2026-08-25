"""`indexbot governance-gate --pr <n>` — the whole per-pull-request GitHub
lane in one process (ADR-4 BD-5, ADR-6 FP-7).

It used to be three jobs of YAML: `indexbot classify-pr` (labels the diff),
`indexbot governance-check` (decides and publishes the
`governance/review-required` commit status plus G-19/G-20 reviewer
assignment), and a separate `contents: write` `arm-auto-merge` job whose
body was `gh pr merge` shell reading `governance-check`'s `disposition`
output. `gate_pull_request_and_sync_auto_merge` below is all three, one call
— the body any hand-written CI, GitHub or GitLab, can invoke directly
instead of wiring three job outputs together itself.

`cli/governance_poll.py`'s sweep calls this exact function once per open
merge request — GitLab has no privileged `pull_request_target` equivalent
(see that module for why), so it must reach the identical disposition by
polling instead of reacting to one event. One implementation, two entry
points, so the two lanes cannot silently diverge on what "gated" means.

**Arm/withdraw** (`sync_auto_merge` below):

- disposition `"success"`: `ForgePort.enable_auto_merge`, bound to the head
  this run classified (`info.head_sha`) — an author push between the read
  and the arm must not arm a revision nothing gated. The adapters treat a
  moved head as "re-classify next tick", not a failure.
- anything else: `ForgePort.withdraw_auto_merge`, idempotently — a PR that
  was never armed (the ordinary human-lane case) costs one no-op read; a PR
  that regressed from machine- to human-lane (a later push added a file the
  author does not own) has whatever an earlier run armed taken back.

## `--no-arm` / `--arm-only`: one decision, two jobs

The default is the whole lane in one call, and that is what GitLab's poller
and any hand-written pipeline should use. GitHub's generated `governance.yml`
splits it across two jobs instead, and the flags are how:

- `governance-gate` job — `--no-arm`. Classifies, labels, gates, publishes
  `disposition`, arms nothing. It therefore never needs `contents: write`.
- `arm-auto-merge` job — `--arm-only --disposition <state> --head-sha <sha>`.
  Holds `contents: write`, classifies nothing, writes no label and no commit
  status: it only replays the arm/withdraw decision the gate already made.

The split is not permission scoping against a compromised bot — both jobs run
the same pinned code, so a bot that could lie about `disposition` could arm on
its own lie either way. What it buys is **fail-closed withdrawal**: the second
job runs on `if: ${{ !cancelled() }}`, so a gate that *errors* (a forge 5xx, a
malformed base-ref root, a `uv` resolution failure) still reaches the
withdraw. An erroring gate publishes an empty `disposition`, which can only
ever take the withdraw branch — never the arm branch — so an already-armed PR
cannot survive on a stale evaluation. A single job that dies mid-gate leaves
that arming standing; two jobs cannot.

`--disposition`/`--head-sha` are read only under `--arm-only`. A full gate
run computes both for itself, so passing them without it is ignored rather
than rejected — there is no configuration in which the ignored value could
be acted on.

**Never checks out the PR head** — same trust boundary
`cli/classify_pr.py`/`cli/governance_check.py` already document: every input
arrives through `ForgePort.get_pull_request_info` and base-ref file reads
(BD-5, FP-7).

Exit code: unchanged (ADR-4 BD-2). A `pending` disposition is not a failure
— the human lane just needs a person — so this always returns `ExitCode.OK`
regardless of disposition; an underlying `IndexBotError` (a malformed
base-ref root, a forge outage) still maps onto its own exit code at
`cli/main.py`'s one chokepoint, same as every other subcommand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ocx_indexbot.cli.classify_pr import apply_change_class, classify_pull_request
from ocx_indexbot.cli.governance_check import gate_pull_request
from ocx_indexbot.exit_codes import ExitCode

from ._common import write_ci_output

if TYPE_CHECKING:
    import argparse

    from ocx_indexbot.core.diff import ChangeClass
    from ocx_indexbot.core.policy import IndexPolicy
    from ocx_indexbot.model import CommitStatusState
    from ocx_indexbot.ports import ForgePort


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `governance-gate`'s CLI surface. `--pr` is a
    trusted GitHub-Actions-expression or poller-supplied value, not
    untrusted `client_payload` data — see `cli/classify_pr.py`'s
    `add_arguments` for why no env-var-indirection discipline applies here."""
    parser.add_argument("--pr", type=int, required=True, help="pull request number to gate")
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument(
        "--no-arm",
        action="store_true",
        help=(
            "gate and publish the disposition, but arm nothing — for a pipeline that "
            "holds the merge scope in a second job (see this module's docstring)"
        ),
    )
    phase.add_argument(
        "--arm-only",
        action="store_true",
        help="arm or withdraw from an already-published --disposition; classify nothing",
    )
    parser.add_argument(
        "--disposition",
        default="",
        help="with --arm-only: the gate's disposition; anything but 'success' withdraws",
    )
    parser.add_argument(
        "--head-sha",
        default="",
        help="with --arm-only: the revision the gate judged, which the arm is bound to",
    )


def sync_auto_merge(number: int, github: ForgePort, *, disposition: str, head_sha: str) -> None:
    """Arm or withdraw the forge's own auto-merge from an already-decided
    `disposition` — the one place that decision is spelled out, for both the
    single-process lane and `--arm-only`'s second job.

    Fail-closed on anything that is not literally `"success"`, the empty
    string included: a gate that errored publishes no disposition at all, and
    that must withdraw whatever an earlier run armed rather than leave it
    standing on a stale evaluation.
    """
    if disposition == "success":
        # Bound to the revision that was classified. Between the gate and this
        # call the author may have pushed; auto-merge must then not arm for a
        # revision nothing gated, so the adapters pass `head_sha` as the
        # forge's own optimistic-concurrency guard and treat a moved head as
        # "re-classify next tick" rather than a failure.
        github.enable_auto_merge(number, head_sha=head_sha)
        return
    # Idempotent: a PR that was never armed (the ordinary human-lane case)
    # costs one no-op read; a PR that regressed from machine- to human-lane
    # has whatever an earlier run armed taken back.
    github.withdraw_auto_merge(number)


def gate_pull_request_and_sync_auto_merge(
    number: int, github: ForgePort, *, policy: IndexPolicy, arm: bool = True
) -> tuple[ChangeClass, CommitStatusState]:
    """Classify, label, gate, and arm/withdraw auto-merge for one pull
    request — the whole per-PR governance lane as a single call.

    Shared by `run` below (one `--pr`) and `cli/governance_poll.py`'s sweep
    (every open merge request) — see this module's docstring.

    `arm=False` is `--no-arm`: everything except the auto-merge write, for the
    caller that holds the merge scope in a separate job and replays the
    decision there through `sync_auto_merge`.
    """
    info = github.get_pull_request_info(number)
    change_class = classify_pull_request(info, github, policy=policy)
    apply_change_class(info, change_class, github)
    state = gate_pull_request(info, change_class, github, policy=policy)
    if arm:
        sync_auto_merge(number, github, disposition=state, head_sha=info.head_sha)
    return change_class, state


def run_arm_only(args: argparse.Namespace, *, github: ForgePort) -> ExitCode:
    """`indexbot governance-gate --pr <n> --arm-only` entry point.

    A separate entry point from `run` because it takes no `policy`, and that
    is deliberate: it classifies nothing, so a base-ref policy read would only
    add a way for the withdraw not to happen. `cli/_wiring.py` routes here
    before it builds one.
    """
    sync_auto_merge(
        cast(int, args.pr),
        github,
        disposition=cast(str, args.disposition),
        head_sha=cast(str, args.head_sha),
    )
    return ExitCode.OK


def run(args: argparse.Namespace, *, github: ForgePort, policy: IndexPolicy) -> ExitCode:
    """`indexbot governance-gate --pr <n> [--no-arm]` entry point.

    `--arm-only` never reaches here — see `run_arm_only`.
    """
    _change_class, state = gate_pull_request_and_sync_auto_merge(
        cast(int, args.pr), github, policy=policy, arm=not cast(bool, args.no_arm)
    )
    write_ci_output("disposition", state)
    return ExitCode.OK
