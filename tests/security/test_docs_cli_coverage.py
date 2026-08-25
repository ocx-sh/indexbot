"""Every registered subcommand is documented, and nothing else is.

`docs/reference/cli.md` is the page an operator reads to write their own
pipeline (`docs/guide/ci.md` links into it lane by lane). A subcommand that
ships without a section there is a command nobody outside this repo can use;
a section left behind by a removed subcommand is worse, because it documents
an invocation that now exits 2. Neither is visible in a diff of either file
alone, so the parser and the page are compared here.

A *duplicated* heading is a third failure mode the set-based checks below
cannot see at all: two `## \\`governance-gate\\`` sections once coexisted, the
first missing half its flags, and `set(DISPATCH) - _documented()` was empty
either way because a set collapses the duplicate before the subtraction ever
runs. `test_no_subcommand_heading_appears_twice` counts instead of sets.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Final

from ocx_indexbot.cli._wiring import DISPATCH

_CLI_REFERENCE: Final[Path] = Path(__file__).resolve().parents[2] / "docs" / "reference" / "cli.md"
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^## `([a-z-]+)`$", re.MULTILINE)

# Anchors the opening sentence's "N subcommands: `a`, `b`, ..." to DISPATCH
# itself, so an added/removed/reordered subcommand fails this test rather
# than leaving a stale count and list for a human to notice. Coupled to that
# one phrase, deliberately: a regex loose enough to survive any rewording of
# the opening paragraph would also be loose enough to stop checking anything.
_OPENING_RE: Final[re.Pattern[str]] = re.compile(
    r"with (\d+) subcommands:\s*(.+?)\.\s*`--version`", re.DOTALL
)


def _headings() -> list[str]:
    return _HEADING_RE.findall(_CLI_REFERENCE.read_text(encoding="utf-8"))


def test_every_subcommand_has_a_reference_section() -> None:
    assert not set(DISPATCH) - set(_headings())


def test_no_reference_section_outlives_its_subcommand() -> None:
    assert not set(_headings()) - set(DISPATCH)


def test_no_subcommand_heading_appears_twice() -> None:
    duplicates = [name for name, count in Counter(_headings()).items() if count > 1]
    assert not duplicates, f"duplicated CLI reference heading(s): {duplicates}"


def test_opening_sentence_count_and_list_match_dispatch() -> None:
    text = _CLI_REFERENCE.read_text(encoding="utf-8")
    match = _OPENING_RE.search(text)
    assert match, (
        "opening sentence no longer reads '...with N subcommands: `a`, `b`, ...'"
        " — update the sentence and this regex together"
    )
    count = int(match.group(1))
    names = re.findall(r"`([a-z-]+)`", match.group(2))
    assert count == len(DISPATCH), f"opening sentence says {count}, DISPATCH has {len(DISPATCH)}"
    assert names == list(DISPATCH), "opening sentence's subcommand list/order drifted from DISPATCH"
