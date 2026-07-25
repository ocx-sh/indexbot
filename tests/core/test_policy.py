"""`core/policy.py` — `.github/index-policy.json` parsing.

Every rejection below is a shape that would otherwise be a *silent* policy
failure: a typo'd key, a scheme-prefixed or upper-cased host, a host carrying
a port. `check_repository_allowlisted` compares against
`urlsplit().hostname`, so each of those would parse fine and then match
nothing — "the bot ignores my policy" is the bug class this parser exists to
turn into an error message.
"""

from __future__ import annotations

import pytest

from indexbot.core.policy import parse_index_policy
from indexbot.errors import ValidationError


def test_parses_the_shipped_shape() -> None:
    assert parse_index_policy(b'{"registry_hosts": ["ghcr.io"]}\n') == frozenset({"ghcr.io"})


def test_parses_multiple_hosts_and_dedups() -> None:
    raw = b'{"registry_hosts": ["ghcr.io", "harbor.corp.internal", "ghcr.io"]}'
    assert parse_index_policy(raw) == frozenset({"ghcr.io", "harbor.corp.internal"})


def test_accepts_a_single_label_host() -> None:
    """Internal registries often have no dot — `harbor` is a legal host."""
    assert parse_index_policy(b'{"registry_hosts": ["harbor"]}') == frozenset({"harbor"})


def test_malformed_json_raises() -> None:
    with pytest.raises(ValidationError, match="malformed JSON"):
        parse_index_policy(b"{not json")


def test_non_object_document_raises() -> None:
    with pytest.raises(ValidationError, match="must be a JSON object"):
        parse_index_policy(b'["ghcr.io"]')


def test_unknown_key_raises() -> None:
    """A typo'd key would otherwise leave the deployment with no policy while
    looking like it had one."""
    with pytest.raises(ValidationError, match="unknown key"):
        parse_index_policy(b'{"registry_host": ["ghcr.io"]}')


def test_missing_key_raises() -> None:
    with pytest.raises(ValidationError, match="missing required"):
        parse_index_policy(b"{}")


def test_non_array_value_raises() -> None:
    with pytest.raises(ValidationError, match="must be an array"):
        parse_index_policy(b'{"registry_hosts": "ghcr.io"}')


def test_empty_array_raises() -> None:
    with pytest.raises(ValidationError, match="at least one host"):
        parse_index_policy(b'{"registry_hosts": []}')


def test_non_string_entry_raises() -> None:
    with pytest.raises(ValidationError, match="is not a registry host"):
        parse_index_policy(b'{"registry_hosts": [42]}')


def test_over_long_entry_raises() -> None:
    raw = b'{"registry_hosts": ["' + b"a" * 254 + b'"]}'
    with pytest.raises(ValidationError, match="is not a registry host"):
        parse_index_policy(raw)


@pytest.mark.parametrize(
    "host",
    [
        "https://ghcr.io",  # scheme
        "GHCR.IO",  # uppercase — hostname comparison is always lowercase
        "ghcr.io:5000",  # port — never part of urlsplit().hostname
        "ghcr.io/ocx-contrib",  # path
        "ghcr.io.",  # trailing dot
        "-ghcr.io",  # leading hyphen
        "",  # empty
    ],
)
def test_non_bare_host_raises(host: str) -> None:
    raw = f'{{"registry_hosts": ["{host}"]}}'.encode()
    with pytest.raises(ValidationError, match="bare lowercase registry host"):
        parse_index_policy(raw)
