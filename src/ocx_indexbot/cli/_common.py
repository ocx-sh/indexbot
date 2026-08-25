"""CLI-layer plumbing: the two writers that hand a result back to CI.

**The sink is chosen by which environment variable exists, not by policy.**
`.github/index-policy.json`'s `ci.forge` declares where the index is *hosted*,
which is what `indexbot ci` generates workflows for; it is not evidence of
where this process is *running*, and the two can differ (a publisher runs
`announce` from a laptop). More decisively, the privileged subcommands read
that policy through a `ForgePort` — so deriving the sink from it would need a
port that had already been chosen. The runner's own variable is the fact, and
it is unambiguous: GitHub Actions sets `$GITHUB_OUTPUT`, and the generated
GitLab job sets `$INDEXBOT_OUTPUT` to the path it declares as its `dotenv`
report.

Pure I/O-boundary helpers — no subcommand business logic here (that is
`cli/<subcommand>.py`). `read_validated_env` (the `repository_dispatch`
`PACKAGE_ID` env-var-indirection reader, ADR-4 BD-4) retired with
`cli/announce.py`'s doorbell pipeline in the fork-PR announce revamp — every
remaining subcommand takes its inputs as CLI args or already-trusted
GitHub-Actions-runner env vars (`cli/_wiring.py`'s `_require_env`), neither
of which needs the untrusted-payload length-cap-then-fullmatch discipline
this module used to also carry.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path
from typing import Final, Literal

_MAX_DELIMITER_ATTEMPTS = 5

_BACKTICK_RUN: Final[re.Pattern[str]] = re.compile(r"`+")


def _random_delimiter() -> str:
    """One random, unguessable multiline-output delimiter."""
    return f"ghadelim_{secrets.token_hex(16)}"


def write_ci_output(name: str, value: str) -> None:
    """Publish `name=value` as a job output, on whichever CI is running.

    GitHub Actions (`$GITHUB_OUTPUT`) always uses the multiline
    delimiter/heredoc form
    (`name<<DELIM\\nvalue\\nDELIM\\n`) so callers never need to special-case
    a value that turns out to contain a newline.

    A fresh random delimiter is generated and rejected if it happens to
    appear verbatim in `value`, retrying up to `_MAX_DELIMITER_ATTEMPTS`
    times — a bound so a pathological value cannot spin forever. With 128
    bits of entropy per attempt a real collision is not expected to ever
    happen; the bound exists so the failure mode is a clear error instead of
    an infinite loop.

    GitLab CI (`$INDEXBOT_OUTPUT`) has no heredoc form to fall back on: a
    `dotenv` report is parsed line by line, so a value carrying a newline
    cannot be expressed and is refused here rather than written out as a file
    GitLab rejects with an unrelated error. Both callers emit one token from a
    closed set, so this is a guard, not a limitation anyone meets.
    """
    github_path = os.environ.get("GITHUB_OUTPUT")
    if github_path:
        _write_actions_output(github_path, name, value)
        return

    gitlab_path = os.environ.get("INDEXBOT_OUTPUT")
    if gitlab_path:
        _write_dotenv_output(gitlab_path, name, value)
        return

    raise RuntimeError("neither GITHUB_OUTPUT nor INDEXBOT_OUTPUT is set")


def _write_actions_output(output_path: str, name: str, value: str) -> None:
    delimiter = _random_delimiter()
    attempts = 1
    while delimiter in value:
        if attempts >= _MAX_DELIMITER_ATTEMPTS:
            raise RuntimeError(
                f"could not find a collision-free delimiter for output {name!r} "
                f"after {_MAX_DELIMITER_ATTEMPTS} attempts"
            )
        delimiter = _random_delimiter()
        attempts += 1

    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


_DOTENV_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:/+-]+")
r"""What may appear in a `dotenv` value.

An allowlist, not a denylist. A `dotenv` report becomes a CI **variable**,
which downstream jobs interpolate into shell — so the question is not "does
this value contain the two characters I thought of" (a newline and a quote,
which is what this checked before) but "is every character in it inert".
Backticks, `$`, `\`, `;` and `'` all reach a shell through a variable.

The set covers everything this bot actually emits: a classification, a gate
disposition, a digest (`sha256:…`), a version tag (`1.0.0+build.1`), a path.
Anything else is a bug in the caller, and a loud one — the caller passes CI
control values here, never free text.
"""


def _write_dotenv_output(output_path: str, name: str, value: str) -> None:
    """One `NAME="value"` line in a GitLab `dotenv` artifact.

    The name is upper-cased because a `dotenv` report becomes a CI variable
    verbatim, and a lowercase `$classification` in a downstream job is both
    unconventional and easy to shadow. The generated job that reads it is the
    only consumer, so both ends move together (WP-6).
    """
    if not _DOTENV_VALUE_RE.fullmatch(value):
        raise RuntimeError(
            f"output {name!r} cannot be written to a GitLab dotenv report: "
            f"its value is outside {_DOTENV_VALUE_RE.pattern}"
        )
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f'{name.upper()}="{value}"\n')


def write_ci_annotation(level: Literal["notice", "error"], title: str, message: str) -> None:
    """Emit one GitHub Actions annotation (`::notice`/`::error`) on stdout, or
    a plain `level: title: message` line on stderr anywhere else.

    Which sink is chosen follows this module's rule: the runner's own variable
    is the fact. `$GITHUB_ACTIONS` is set by every GitHub Actions job and by
    nothing else, so a GitLab job (or a laptop) gets the readable line instead
    of a literal `::error` string it has no parser for.

    stdout, not stderr, for the workflow-command form — that is where the
    generated workflow's own `echo` put it, and where GitHub documents it.
    Nothing else this subcommand writes goes to stdout, so the annotation *is*
    its machine-readable result (PY-CLI-02).

    **`title` and `message` are caller-owned literals, never PR content.** A
    workflow command is newline-terminated and its arguments are parsed
    positionally, so untrusted text here can forge an annotation or escape the
    command entirely. Untrusted detail goes to `write_ci_summary`'s fenced
    block instead (ADR-4 BD-4).
    """
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level} title={title}::{message}")
        return
    print(f"{level}: {title}: {message}", file=sys.stderr)


def _fence(body: str) -> str:
    """A backtick fence guaranteed to be longer than any run inside `body`.

    CommonMark closes a fenced block on the first run of at least as many
    backticks as opened it, so a fixed three-backtick fence lets untrusted
    content containing one break out of the block and back into markdown the
    summary page renders. Three minimum, one more than the longest run
    otherwise.
    """
    longest = max((len(run) for run in _BACKTICK_RUN.findall(body)), default=0)
    return "`" * max(3, longest + 1)


def write_ci_summary(heading: str, reason: str, detail: str | None = None) -> None:
    """Append a `## heading\\n\\nreason\\n` markdown block to `$GITHUB_STEP_SUMMARY`.

    The observability floor (register §5, BCR #176): a publisher-visible
    failure must never exit with only a bare stderr line — it also surfaces a
    structured, human-readable reason on the workflow run's job summary, the
    page a publisher actually reads.

    `detail` — when given — follows as a fenced code block. That is where any
    text derived from pull-request content belongs: fenced, never in a
    `::error` title or any other command-evaluated position (ADR-4 BD-4).

    Unlike `write_ci_output`'s hard requirement, an unset-or-empty
    `$GITHUB_STEP_SUMMARY` is **not** an error: this helper is on an
    error-reporting path, and a missing summary sink must never mask the
    original failure it is trying to surface. In that case it no-ops on the
    file and emits a single line to stderr instead.

    That fallback is also the whole GitLab implementation, deliberately.
    GitLab has no job-summary surface; its equivalent is the job log, which is
    exactly where stderr lands and exactly the page a publisher opens from a
    failed pipeline. The alternative — posting the reason as an MR note —
    would need a write-scoped token and an MR number inside the one code path
    that runs when *anything* failed, policy loading included.
    """
    block = "" if detail is None else f"\n{_fence(detail)}\n{detail}\n{_fence(detail)}\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        print(f"{heading}: {reason}", file=sys.stderr)
        if detail is not None:
            print(detail, file=sys.stderr)
        return

    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write(f"## {heading}\n\n{reason}\n{block}")
