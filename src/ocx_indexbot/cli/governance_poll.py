"""`indexbot governance-poll` — the governance lane for a forge with no
privileged pull-request trigger.

GitHub Actions has `pull_request_target`: a job that runs base-authored
workflow code, with the base repository's secrets, in response to a fork's
pull request. `.github/workflows/governance.yml` is built on it (ADR-4 BD-5,
ADR-6 FP-7).

**GitLab has no equivalent, and the near-misses are worse than nothing.** A
fork's merge-request pipeline runs in the fork, under the fork's variables —
which is the right trust boundary, and the same one GitHub's plain
`pull_request` gives. The features that put the *parent's* variables on a
fork MR all work by running the fork's own `.gitlab-ci.yml` in the parent's
context, which is precisely the compromise `pull_request_target` exists to
avoid. So on GitLab the privileged actor cannot be MR-driven. It is a
schedule on the parent's default branch:

- parent-authored config, because a scheduled pipeline runs the default
  branch's `.gitlab-ci.yml` and never a fork's;
- parent-held token, because it is the parent's own scheduled pipeline;
- and it never checks out or executes merge-request content, exactly like
  `governance-gate` — every input arrives through the API
  (`ForgePort.get_pull_request_info`, and base-ref file reads).

Per open MR it runs the exact same body `cli/governance_gate.py`'s single-PR
gate runs — classify and label, gate and assign review, then arm or
withdraw auto-merge — see that module for what each step does and why. The
poll lane differs only in *when* it runs (a schedule sweeping every open MR,
never an event on one) and *why* it exists at all (no MR-driven privileged
trigger on GitLab): never in what a merge request's outcome is. There is
nowhere else to put the arm/withdraw on GitLab: no MR event means no
event-driven job to hold the wider scope, and the poller already holds a
token that can merge.

**One MR's failure never ends the sweep.** A malformed base-ref root or a
404 on one merge request must not leave every other open MR ungated; each is
caught, reported, and the run exits with the worst code it saw. A poller's
retry is its next tick.

That guard catches `Exception`, not `IndexBotError`, and the difference is
not theoretical: a raw `httpx.HTTPStatusError` from one merge request's
commit-status POST ended a whole production sweep, leaving every later MR
ungated. The adapters now wrap their own failures (`errors.ForgeError`), so
this is the second line of defence rather than the first — but the sweep's
blast radius must be one merge request no matter what the layer below
forgets, so anything unrecognised is reported under its own type name and
counted as a validation failure.

**Latency is the trade, and it is bounded.** The gate is not a race: the
commit status starts absent, and an absent status is already blocking
(measured on gitlab.com Free, 2026-08-24 — `detailed_merge_status` reads
`ci_must_pass` with no status at all). So an MR opened between two ticks is
unmergeable until the poller reaches it, never briefly mergeable. Fail-closed
by construction, which is why this design is acceptable at all.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from ocx_indexbot.cli.governance_gate import gate_pull_request_and_sync_auto_merge
from ocx_indexbot.errors import IndexBotError
from ocx_indexbot.exit_codes import ExitCode

if TYPE_CHECKING:
    import argparse

    from ocx_indexbot.core.policy import IndexPolicy
    from ocx_indexbot.ports import ForgePort


def _gate_one(number: int, github: ForgePort, *, policy: IndexPolicy) -> str:
    """One merge request's outcome line — via the exact same body
    `cli/governance_gate.py`'s single-PR gate runs. See that module for what
    "gate" includes (classify, label, set the commit status, assign review,
    arm or withdraw auto-merge); this wrapper only formats the sweep's
    per-MR stderr line.
    """
    change_class, state = gate_pull_request_and_sync_auto_merge(number, github, policy=policy)
    return f"{change_class} -> {state}"


def run(args: argparse.Namespace, *, github: ForgePort, policy: IndexPolicy) -> ExitCode:
    """`indexbot governance-poll` entry point — takes no arguments.

    The exit code is the worst one any single merge request produced, ordered
    by the `ExitCode` values themselves. The ordering carries no meaning
    beyond "not zero": a sweep that failed anywhere must not report success,
    and the per-MR stderr lines are what says which one and why.
    """
    del args  # no CLI surface: the sweep's scope is "every open MR", always
    worst = ExitCode.OK
    numbers = github.list_open_pull_requests()
    print(f"governance-poll: {len(numbers)} open merge request(s)", file=sys.stderr)
    for number in numbers:
        try:
            outcome = _gate_one(number, github, policy=policy)
        except IndexBotError as exc:
            print(f"governance-poll: #{number}: {exc}", file=sys.stderr)
            worst = max(worst, exc.exit_code)
        except Exception as exc:  # deliberate: one MR must never end the sweep
            print(f"governance-poll: #{number}: {type(exc).__name__}: {exc}", file=sys.stderr)
            worst = max(worst, ExitCode.VALIDATION_FAILURE)
        else:
            print(f"governance-poll: #{number}: {outcome}", file=sys.stderr)
    return worst


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """No arguments. Declared so `cli/main.py`'s populator table stays total
    over the registered subcommands, the same reason `cli/schema_cmd.py`
    declares one."""
    del parser
