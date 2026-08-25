from __future__ import annotations

import pytest

from ocx_indexbot.core.maintainers import parse_maintainers
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.model import Owner


def test_parse_maintainers_single_entry() -> None:
    raw = b"maintainers:\n  - login: michael-herwig\n    id: 3511590\n"
    assert parse_maintainers(raw) == (Owner(login="michael-herwig", id=3511590),)


def test_parse_maintainers_multiple_entries() -> None:
    raw = b"maintainers:\n  - login: alice\n    id: 1\n  - login: bob\n    id: 2\n"
    assert parse_maintainers(raw) == (
        Owner(login="alice", id=1),
        Owner(login="bob", id=2),
    )


def test_parse_maintainers_empty_list() -> None:
    assert parse_maintainers(b"maintainers:\n") == ()


def test_parse_maintainers_skips_blank_lines_and_comments() -> None:
    raw = (
        b"# maintainers.yml\n"
        b"maintainers:\n"
        b"\n"
        b"  # primary maintainer\n"
        b"  - login: alice\n"
        b"    id: 1\n"
        b"\n"
    )
    assert parse_maintainers(raw) == (Owner(login="alice", id=1),)


def test_parse_maintainers_missing_top_key_raises() -> None:
    with pytest.raises(ValidationError, match="top-level 'maintainers:' key"):
        parse_maintainers(b"- login: alice\n  id: 1\n")


def test_parse_maintainers_empty_file_raises() -> None:
    with pytest.raises(ValidationError, match="top-level 'maintainers:' key"):
        parse_maintainers(b"")


def test_parse_maintainers_odd_entry_count_raises() -> None:
    with pytest.raises(ValidationError, match="malformed maintainer entry"):
        parse_maintainers(b"maintainers:\n  - login: alice\n")


def test_parse_maintainers_malformed_github_line_raises() -> None:
    with pytest.raises(ValidationError, match="entry 0 is malformed"):
        parse_maintainers(b"maintainers:\n  github: alice\n    id: 1\n")


def test_parse_maintainers_malformed_github_id_line_raises() -> None:
    with pytest.raises(ValidationError, match="entry 0 is malformed"):
        parse_maintainers(b"maintainers:\n  - login: alice\n    id: not-a-number\n")


def test_the_pre_0_5_0_spelling_still_parses() -> None:
    """`adr_forge_neutral_owners.md` D2's read-both rule, on this side of the
    rename: an index whose `maintainers.yml` still says `github:`/`github_id:`
    keeps requesting reviewers rather than failing the human lane closed."""
    raw = b"maintainers:\n  - github: alice\n    github_id: 1\n"
    assert parse_maintainers(raw) == (Owner(login="alice", id=1),)


def test_the_two_spellings_may_be_mixed_across_entries() -> None:
    """No emit side exists for this file — nothing writes it — so there is no
    derived pair to disagree with and each entry is parsed on its own. That is
    the one place this differs from the wire codec."""
    raw = b"maintainers:\n  - login: alice\n    github_id: 1\n  - github: bob\n    id: 2\n"
    assert parse_maintainers(raw) == (Owner(login="alice", id=1), Owner(login="bob", id=2))
