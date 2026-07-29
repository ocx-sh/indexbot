"""Version-tag ordering.

`find_latest_version` is a faithful port of `ocx/scripts/catalog-generate.py`'s
function of the same name (byte-equal comparison semantics — verified against
the source, no separate "yank-exclusion" logic existed there; that part is
new, per ADR-1's yank semantics). `is_build_pinned_version` is new — the
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

_OCX_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:([a-z][a-z0-9.]*)-)?(?:0|[1-9][0-9]*)"
    r"(?:\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9a-zA-Z]+)?(?:[_+]([0-9a-zA-Z]+))?)?)?$"
)
"""The *whole* version grammar, as `Version::parse` in
`crates/ocx_lib/src/package/version.rs` spells it — prerelease and build
segments included.

Deliberately **not** `_VERSION_RE` above. That one predates prerelease/build
support and stops at `major.minor.patch`, which is correct for its one caller
(`find_latest_version` compares an int tuple) and wrong here: it rejects
`slim-3.28.1-rc1` outright, so reusing it would silently drop `slim` from a
package whose only slim tags carry a prerelease — and it cannot express a
build fragment at all, which is the whole distinction
`is_build_pinned_version` turns on.

Two groups are captured, both of which a caller reads: the variant prefix
(`variant_names`) and the build fragment (`is_build_pinned_version`). `[_+]`
mirrors `Version::parse`'s accepted separators even though only `_` is legal
in an OCI tag (`schema/root.schema.json`'s `propertyNames` pattern), so the
two grammars stay comparable line for line.
"""


def _parse_version(tag: str) -> re.Match[str] | None:
    """`tag` as `Version::parse` sees it, or `None` if it is not a version.

    `latest` is refused as a variant prefix exactly as the Rust parser refuses
    it, so `latest-3.28.1` is not a version here either — it is an opaque tag
    that happens to contain digits.
    """
    match = _OCX_VERSION_RE.fullmatch(tag)
    if match is None or match.group(1) == "latest":
        return None
    return match


def is_build_pinned_version(tag: str) -> bool:
    """True iff `tag` is a version carrying a build fragment — `3.28.1_20260216`.

    That fragment is what makes a tag immutable. `ocx package push` writes the
    build-tagged version once and then *repoints* every rolling ancestor at it
    (`crates/ocx_lib/src/package/cascade.rs`: `3.28.1_<build>` cascades to
    `3.28.1`, `3.28`, `3`, `latest`), so the ancestors are exactly the tags
    expected to move and the build-tagged leaf is exactly the one that is not.
    The variant prefix and the prerelease segment ride along untouched —
    `slim-3.12.13_20260728` and `1.0.0-rc1_20260728` are pinned too, and their
    build-less parents `slim-3.12.13` / `1.0.0-rc1` are the rolling targets.

    Anything that is not a version at all (`latest`, `nightly`, a bare variant
    name like `slim`) is `False`: a publisher's opaque tag carries no promise
    of immutability, so flagging its movement would be a false alarm.
    """
    match = _parse_version(tag)
    return match is not None and match.group(2) is not None


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
                if (match := _parse_version(tag)) is not None and match.group(1) is not None
            }
        )
    )


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
