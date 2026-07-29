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
    from collections.abc import Iterable, Mapping

    from indexbot.model import TagEntry

_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:([a-z][a-z0-9.]*)-)?((0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?)?)$"
)

_VARIANT_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:([a-z][a-z0-9.]*)-)?(?:0|[1-9][0-9]*)"
    r"(?:\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9a-zA-Z]+)?(?:[_+][0-9a-zA-Z]+)?)?)?$"
)
"""The *whole* version grammar, as `Version::parse` in
`crates/ocx_lib/src/package/version.rs` spells it — prerelease and build
segments included.

Deliberately **not** `_VERSION_RE` above. That one predates prerelease/build
support and stops at `major.minor.patch`, which is correct for its two callers
(`find_latest_version` compares an int tuple; `is_full_release_version` gates
anomaly checking on pinned releases) and wrong here: it rejects
`slim-3.28.1-rc1` outright, so reusing it would silently drop `slim` from a
package whose only slim tags carry a prerelease. Widening `_VERSION_RE`
instead would newly admit those tags as "full release versions" and change
what `core/anomaly.py` checks — a different concern, not this one.

Only the variant prefix is captured: nothing downstream of the prefix needs
naming, and an uncaptured group cannot be read by accident.
"""


def variant_names(tags: Iterable[str]) -> tuple[str, ...]:
    """The distinct variant names observed across `tags`, sorted, deduplicated.

    A tag contributes a variant only when it is a version *and* carries a
    prefix, so `latest`, an unprefixed `3.28.1`, and any tag that is not a
    version at all contribute nothing. The default variant is the *absence* of
    a prefix and therefore has no name here.

    A bare rolling tag (`slim`) is invisible to this function: it is not a
    version, and inferring "`slim` is a variant because `slim-3.28.1` exists"
    is a display-layer inference (`site/.vitepress/theme/utils/version.ts`
    makes it in a second pass), not part of the derivation the root records.

    The one Python implementation of the rule: `core/regenerate.py` records the
    result as `PackageRoot.variants` and nothing else re-derives it. Its Rust
    counterpart is `ocx_lib::package::version::variant_names`, which
    `ocx index list --variants` and `ocx package announce` share the same way.
    """
    return tuple(
        sorted(
            {
                match.group(1)
                for tag in tags
                if (match := _VARIANT_RE.fullmatch(tag)) is not None
                and match.group(1) is not None
                and match.group(1) != "latest"
            }
        )
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
