"""Lexical shapes shared by the policy parser and the entry validator.

`core/policy.py` and `core/validate_entry.py` both need the same three
alphabets — a bare registry host, a namespace segment, a package segment —
and before 0.2.0 each kept its own copy. That was survivable while the policy
file held nothing but hosts; it stopped being survivable once the policy file
started declaring the index's `name` (host grammar) and its
`reserved_namespaces` (namespace grammar), because `validate_entry` already
imports `policy` for `INDEX_POLICY_PATH` and the reverse import would close a
cycle.

This module is constants only — no branches, no I/O, nothing to mock. It is
the single home for the alphabets; the *compiled* regexes that combine them
stay in their own modules, so BD-4's two-regex rule (the fixed package-id
grammar is never the same object as the N-segment OCI repository grammar)
still holds.
"""

from __future__ import annotations

import re
from typing import Final

# --- registry hosts, and the index's own `name` -----------------------------

HOST_MAX_LENGTH: Final[int] = 253  # RFC 1035 total domain-name length

_HOST_LABEL = r"[a-z0-9]+(?:-+[a-z0-9]+)*"

HOST_RE: Final[re.Pattern[str]] = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*")
"""A bare, lowercase registry host: DNS labels only, no scheme, no port, no
path, no trailing dot.

Deliberately strict, because every rejected shape here would otherwise be a
silent never-match: `check_repository_allowlisted` compares against
`urlsplit(repository).hostname`, which is always lowercased and never carries
a port — so a policy entry of `https://harbor.corp`, `Harbor.Corp` or
`harbor.corp:5000` would parse fine, allowlist nothing, and read as "the bot
ignores my policy". A registry served on a non-default port is still
allowlisted by its bare host (`harbor.corp` admits
`oci://harbor.corp:5000/team/tool`). A single label (`harbor`) is legal —
internal registries often have no dot.

The index's own `name` (`ocx.sh`, `acme.corp`) uses this same grammar, and
not by coincidence: it is the registry-namespace key an ocx client configures
as `[registries."<name>"] index = …`, so anything this rejects is a name no
client could route to.
"""

# --- package-id segments ----------------------------------------------------

NAMESPACE_MAX_LENGTH: Final[int] = 39  # ADR-2 ND-3
PACKAGE_MAX_LENGTH: Final[int] = 100  # ADR-2 ND-3

NAMESPACE_SHAPE: Final[str] = r"[a-z0-9](?:-?[a-z0-9])*"
"""First segment of a package id. Tighter than `PACKAGE_SHAPE`: no dots and
no underscores, because this segment is also a path component directly under
`p/` and a top-level route on the served site."""

PACKAGE_SHAPE: Final[str] = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
"""Every segment after the first — the OCI repository-component alphabet."""

NAMESPACE_RE: Final[re.Pattern[str]] = re.compile(NAMESPACE_SHAPE)
"""The first segment of a package id, and — the same alphabet, the same
reason — a policy `reserved_namespaces` entry, which is compared against one
and so must be able to *be* one."""

PACKAGE_RE: Final[re.Pattern[str]] = re.compile(PACKAGE_SHAPE)
"""Every segment after the first."""


def package_id_max_length(name_segments: int) -> int:
    """Longest legal package id under an index declaring `name_segments`.

    The first segment takes `NAMESPACE_MAX_LENGTH`, every later one
    `PACKAGE_MAX_LENGTH` plus its separator. At the two-segment default this
    is ADR-2 ND-3's 140, unchanged.

    Exists so `parse_package_id` can cap length *before* running any regex
    (BD-4's untrusted-input order) on an index whose depth is configuration
    rather than a constant.
    """
    return NAMESPACE_MAX_LENGTH + (name_segments - 1) * (1 + PACKAGE_MAX_LENGTH)
