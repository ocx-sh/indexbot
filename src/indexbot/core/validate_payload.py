"""`repository_dispatch` `client_payload` validation — the `PACKAGE_ID` shape.

`PACKAGE_ID_RE` is the OCX **package-id** regex: exactly two lowercase
segments (`<namespace>/<package>`) joined by a single `/` (ADR-2 ND-2/ND-3).
It is structurally distinct from `core/validate_entry.py`'s
`OCI_REPOSITORY_RE` (an N-segment OCI repository path) — the two-regex rule
(ADR-4 BD-4) forbids ever sharing a compiled pattern or a "guess which shape"
helper between them.

`parse_package_id` is the concrete BD-4 length-cap-then-fullmatch algorithm:
reject on raw length *before* any regex work, `fullmatch` only (never
`match`/`search`, which would silently accept a valid prefix followed by
injected garbage), then re-check each already-`fullmatch`-guaranteed segment
against its own per-segment cap — the combined regex is deliberately silent
on per-segment length, so a value can satisfy the 140-char combined budget
while still violating ADR-2 ND-3's 39/100 per-segment caps (e.g. a 1-char
namespace plus a 138-char package).

`cli/announce.py` is this function's only caller, reached via
`cli/_common.read_validated_env` (which itself only does the generic
length-cap-then-fullmatch env-var read; `PACKAGE_ID_RE` and
`PACKAGE_ID_MAX_LENGTH` are supplied by that caller from here) on the raw
`PACKAGE_ID` env var before anything else touches it.
"""

from __future__ import annotations

import re
from typing import Final

from indexbot.errors import ValidationError
from indexbot.model import PackageId

PACKAGE_ID_MAX_LENGTH: Final[int] = 140  # ADR-2 ND-3: 39 (namespace) + 1 ("/") + 100 (package)
_NAMESPACE_MAX_LENGTH: Final[int] = 39
_PACKAGE_MAX_LENGTH: Final[int] = 100

_NAMESPACE_SHAPE = r"[a-z0-9](?:-?[a-z0-9])*"
_PACKAGE_SHAPE = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
PACKAGE_ID_RE: Final[re.Pattern[str]] = re.compile(rf"^{_NAMESPACE_SHAPE}/{_PACKAGE_SHAPE}$")


def parse_package_id(raw: str) -> PackageId:
    """Validate and parse `raw` as an OCX `<namespace>/<package>` id.

    Raises `ValidationError` if `raw` exceeds `PACKAGE_ID_MAX_LENGTH`
    (checked first, before any regex evaluation — BD-4), does not
    `fullmatch` `PACKAGE_ID_RE`, or (having matched the combined shape)
    splits into a namespace or package segment exceeding its own
    per-segment cap (ADR-2 ND-3).
    """
    if len(raw) > PACKAGE_ID_MAX_LENGTH:
        raise ValidationError(f"package id exceeds max length {PACKAGE_ID_MAX_LENGTH} characters")
    if PACKAGE_ID_RE.fullmatch(raw) is None:
        raise ValidationError(f"package id {raw!r} does not match the expected shape")

    # A `PACKAGE_ID_RE` fullmatch guarantees exactly one "/" in `raw`, which
    # is what makes this split safe.
    namespace, package = raw.split("/", 1)
    if len(namespace) > _NAMESPACE_MAX_LENGTH:
        raise ValidationError(
            f"namespace {namespace!r} exceeds max length {_NAMESPACE_MAX_LENGTH} characters"
        )
    if len(package) > _PACKAGE_MAX_LENGTH:
        raise ValidationError(
            f"package {package!r} exceeds max length {_PACKAGE_MAX_LENGTH} characters"
        )
    return PackageId(namespace=namespace, package=package)
