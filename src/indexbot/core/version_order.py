"""Version-tag ordering.

`find_latest_version` is a faithful port of `ocx/scripts/catalog-generate.py`'s
function of the same name (byte-equal comparison semantics — verified against
the source, no separate "yank-exclusion" logic existed there; that part is
new, per ADR-1's yank semantics). `is_full_release_version` is new — the
pinned-vs-floating predicate `core/anomaly.py` (WP2-B) reuses to decide which
tags are anomaly-checked.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

    from indexbot.model import TagEntry

_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:([a-z][a-z0-9.]*)-)?((0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?)?)$"
)


def is_full_release_version(tag: str) -> bool:
    """True iff `tag` is an unprefixed, fully-qualified 3-component version.

    `_VERSION_RE` matches, the variant-prefix group is absent, and both the
    minor and patch groups are present. `latest`, a bare major (`3`), a
    major.minor (`3.28`), and any variant-prefixed tag are all `False`.
    """
    match = _VERSION_RE.fullmatch(tag)
    if match is None:
        return False
    return match.group(1) is None and match.group(4) is not None and match.group(5) is not None


def find_latest_version(tags: Mapping[str, TagEntry]) -> str | None:
    """Highest version among tags that are not "latest", unprefixed, and not yanked.

    Comparison is by the parsed `(major, minor, patch)` int tuple, missing
    components treated as absent (not zero) for tuple comparison purposes —
    matches the ported function's `tuple(int(x) for x in m.group(2).split(".")
    if x)` behavior exactly. Returns `None` if no eligible tag exists.
    """
    best_tag: str | None = None
    best_parts: tuple[int, ...] = ()

    for tag, entry in tags.items():
        if tag == "latest":
            continue
        if entry.yanked is not None:
            continue
        match = _VERSION_RE.fullmatch(tag)
        if match is None:
            continue
        if match.group(1) is not None:
            continue
        parts = tuple(int(x) for x in match.group(2).split(".") if x)
        if parts > best_parts:
            best_parts = parts
            best_tag = tag

    return best_tag
