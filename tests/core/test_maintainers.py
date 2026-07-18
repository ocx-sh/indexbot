from __future__ import annotations

import pytest

from indexbot.core.maintainers import parse_maintainers
from indexbot.errors import ValidationError
from indexbot.model import Owner


def test_parse_maintainers_single_entry() -> None:
    raw = b"maintainers:\n  - github: michael-herwig\n    github_id: 3511590\n"
    assert parse_maintainers(raw) == (Owner(github="michael-herwig", github_id=3511590),)


def test_parse_maintainers_multiple_entries() -> None:
    raw = b"maintainers:\n  - github: alice\n    github_id: 1\n  - github: bob\n    github_id: 2\n"
    assert parse_maintainers(raw) == (
        Owner(github="alice", github_id=1),
        Owner(github="bob", github_id=2),
    )


def test_parse_maintainers_empty_list() -> None:
    assert parse_maintainers(b"maintainers:\n") == ()


def test_parse_maintainers_skips_blank_lines_and_comments() -> None:
    raw = (
        b"# maintainers.yml\n"
        b"maintainers:\n"
        b"\n"
        b"  # primary maintainer\n"
        b"  - github: alice\n"
        b"    github_id: 1\n"
        b"\n"
    )
    assert parse_maintainers(raw) == (Owner(github="alice", github_id=1),)


def test_parse_maintainers_missing_top_key_raises() -> None:
    with pytest.raises(ValidationError, match="top-level 'maintainers:' key"):
        parse_maintainers(b"- github: alice\n  github_id: 1\n")


def test_parse_maintainers_empty_file_raises() -> None:
    with pytest.raises(ValidationError, match="top-level 'maintainers:' key"):
        parse_maintainers(b"")


def test_parse_maintainers_odd_entry_count_raises() -> None:
    with pytest.raises(ValidationError, match="malformed maintainer entry"):
        parse_maintainers(b"maintainers:\n  - github: alice\n")


def test_parse_maintainers_malformed_github_line_raises() -> None:
    with pytest.raises(ValidationError, match="entry 0 is malformed"):
        parse_maintainers(b"maintainers:\n  github: alice\n    github_id: 1\n")


def test_parse_maintainers_malformed_github_id_line_raises() -> None:
    with pytest.raises(ValidationError, match="entry 0 is malformed"):
        parse_maintainers(b"maintainers:\n  - github: alice\n    github_id: not-a-number\n")
