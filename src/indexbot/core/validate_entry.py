"""Schema-adjacent semantic checks on a committed `PackageRoot` / CAS object.

Everything JSON Schema *can* express (`schema/root.schema.json`,
`schema/image-index.schema.json`) runs via `check-jsonschema`, never
imported here (ADR-4 BD-1). This module owns the checks a schema cannot
express: path<->name derivation (G-02), repository host allowlisting (G-03,
checked before any network intent — SSRF ordering; the allowlist itself is
this deployment's committed policy, `core/policy.py`, passed in by
`cli/_wiring.py` rather than hardcoded here), reserved-namespace
rejection (ADR-2 ND-4), reserved-tag rejection (D7 — the one implementation
`core/observe.py`'s sweep exclusion imports rather than restating), digest-hex
`fullmatch` before any path join, content-digest self-consistency (CAS
integrity), dangling-reference detection, the `PackageRoot` <-> `dict` codec
every other module reuses (CONTRACTS.md §5.6) rather than hand-rolling a second
encoder, and `cas_relpath` — the one CAS relative-path builder every writer
(`core/render.py`, `cli/reconcile.py`) reuses rather than hand-rolling the
`p/<ns>/<pkg>/o/sha256/<hex>.<ext>` shape a second time (relocated here from
the now-deleted `core/catalog_md.py`, site redesign plan_site_redesign
WP-bot — `core/validate_entry.py` is this repo's one shared foundation
module, not `core/catalog_md.py`, whose only other export was VitePress
wrapper-page markdown the site redesign's dynamic routes retire).

`OCI_REPOSITORY_RE` and `PACKAGE_ID_RE` below are two structurally distinct
constants (ADR-4 BD-4's two-regex rule) — one governs the physical,
N-segment OCI repository grammar; the other governs the logical,
fixed-two-segment package id. Never shared, never guessed at runtime.
`parse_package_id`/`PACKAGE_ID_RE`/`PACKAGE_ID_MAX_LENGTH` re-home here
(fork-PR announce revamp) from the now-deleted `core/validate_payload.py` —
this module was already `PACKAGE_ID_RE`'s only in-tree consumer beyond the
callers that import `parse_package_id` directly, so it is the sensible
single home for both regexes rather than a standalone module for one
function.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final, cast
from urllib.parse import urlsplit

from indexbot.core.policy import INDEX_POLICY_PATH
from indexbot.core.version_order import variant_names
from indexbot.errors import AnomalyError, ValidationError
from indexbot.model import (
    Desc,
    Owner,
    PackageId,
    PackageRoot,
    TagEntry,
    Upstream,
    Yank,
)

# --- N-segment OCI repository grammar (BD-4's two-regex rule: never shared
# with PACKAGE_ID_RE below, the fixed-two-segment package-id shape). --------
_COMPONENT = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
OCI_REPOSITORY_RE: Final[re.Pattern[str]] = re.compile(rf"^{_COMPONENT}(?:/{_COMPONENT})*$")

_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[a-f0-9]{64}")

# --- fixed-two-segment package-id grammar (BD-4's two-regex rule: never
# shared with OCI_REPOSITORY_RE above). Re-homed from the deleted
# core/validate_payload.py. ---------------------------------------------
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


RESERVED_NAMESPACE_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        # Control paths — top-level directories in the index git tree and/or
        # top-level URL paths on the colocated index.ocx.sh deployment.
        "p",
        "o",
        "docs",
        "assets",
        "config",
        "schema",
        "api",
        "static",
        "data",
        # Brand — OCX's own project/org identities.
        "ocx",
        "ocx-sh",
        "ocx-contrib",
        "ocx-rs",
        # Generic/ambiguous — words implying a privileged or non-existent-
        # vendor status the two-level namespace model explicitly refuses to
        # grant (ADR-2 ND-2).
        "admin",
        "root",
        "system",
        "std",
        "core",
        "official",
        "public",
        "test",
        "example",
        "internal",
    }
)
"""ADR-2 ND-4's reserved segment list — checked against both the namespace
and package positions of a `PackageId` (the two-segment package-id shape does
not otherwise distinguish which position collides)."""

RESERVED_BRAND_SEGMENTS: Final[frozenset[str]] = frozenset(
    {"ocx", "ocx-sh", "ocx-contrib", "ocx-rs"}
)
"""The subset of `RESERVED_NAMESPACE_SEGMENTS` naming OCX's own brand — the
only segments `check_namespace_not_reserved`'s `allow_reserved=True`
carve-out ever admits (ADR-2 ND-10's first-party `ocx/cli` example vs. ND-4's
unconditional reservation; policy call is PR-gated, this is the mechanism
only). Control-path segments (`p`, `o`, ...) and generic/ambiguous segments
(`admin`, `root`, ...) stay unconditionally reserved regardless of this flag
— never widen this set without a reviewed PR."""


def check_name_matches_path(package_id: PackageId, root: PackageRoot) -> None:
    """G-02: `root.name` must equal the path-derived logical name."""
    expected = f"ocx.sh/{package_id.namespace}/{package_id.package}"
    if root.name != expected:
        raise ValidationError(
            f"root name {root.name!r} does not match path-derived name {expected!r} (G-02)"
        )


def check_superseded_by(root: PackageRoot) -> None:
    """`root.superseded_by`, when set, must be a shape-valid
    `<namespace>/<package>` id (reused via this module's own
    `parse_package_id` — never a second hand-rolled regex, ADR-4 BD-4) that
    does not name `root` itself.

    `root.superseded_by is None` is a no-op — a package that has not been
    superseded carries no constraint here.

    Deliberately **not** checked (do not silently add these — they are
    scope decisions, not oversights):

    - **No status coupling**: `superseded_by` is independent of
      `root.status` — a package can name a successor while still `active`,
      or be `deprecated`/`yanked` with no successor at all. `superseded ≠
      deprecated`.
    - **No existence/reserved-namespace check**: the named successor is not
      required to already exist as a committed root, nor is it checked
      against `RESERVED_NAMESPACE_SEGMENTS` — a dangling or not-yet-claimed
      successor is allowed, the same free-text-pointer treatment
      `deprecated_message` already gets.
    """
    if root.superseded_by is None:
        return
    try:
        parse_package_id(root.superseded_by)
    except ValidationError as exc:
        raise ValidationError(
            f"superseded_by {root.superseded_by!r} is not a valid <namespace>/<package> id: {exc}"
        ) from exc
    this_id = root.name.removeprefix("ocx.sh/")
    if root.superseded_by == this_id:
        raise ValidationError(
            f"superseded_by {root.superseded_by!r} cannot reference its own package ({root.name!r})"
        )


_UPSTREAM_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
"""Z-anchored, fixed-width ISO 8601 UTC timestamp — the exact shape
`adapters/system_clock.py`'s `SystemClock.now_iso8601` always writes.
`schema/root.schema.json`'s `tagEntry.observed`/`yanked.at` pattern mirrors
this constant; kept as two literal strings, not a shared import, so the
schema stays `check-jsonschema`-standalone (ADR-4 BD-1: schema never imports
bot code)."""


def check_upstream_repository_url_scheme(root: PackageRoot) -> None:
    """Input-boundary guard (review-round-1 finding #1): `upstream.repository_url`,
    when present, must have scheme `http` or `https`.

    `schema/root.schema.json`'s `format: uri` alone admits `javascript:`,
    `data:`, or scheme-less values — the site renders this field as an
    unescaped `href` (client-side guard is the parallel site-redesign
    branch's concern; this closes the server-side input boundary). Pure
    string parsing only (`urllib.parse`, mirroring
    `check_repository_allowlisted`'s `urlsplit` pattern) — never touches a
    `RegistryPort`.

    `root.upstream is None` or `root.upstream.repository_url is None` is a
    no-op — no upstream, or an upstream with no repository URL, carries no
    constraint here.
    """
    if root.upstream is None or root.upstream.repository_url is None:
        return
    scheme = urlsplit(root.upstream.repository_url).scheme
    if scheme not in _UPSTREAM_URL_SCHEMES:
        raise ValidationError(
            f"upstream.repository_url {root.upstream.repository_url!r} must use "
            f"http or https scheme, got {scheme!r}"
        )


def check_variants_match_tags(root: PackageRoot) -> None:
    """A *present* `variants` must equal the derivation over this root's own
    `tags`. An absent or empty one is always accepted.

    What this guarantees: no root asserts a variant set its own tags cannot
    produce. Neither existing gate can express that — the schema constrains
    the shape but not the relationship, and the byte gate is a parse ->
    serialize round-trip, so it accepts any well-formed value a human typed.
    Without this check a hand-authored root could claim a variant set no
    derivation could produce and survive until the next announce silently
    overwrote it. That is the anti-tamper property, and it is untouched.

    Why absence is safe: an absent field asserts nothing, so there is nothing
    to tamper with, and `core/render.py` derives the catalog's `variants`
    from `tags` regardless of what the root stores. Requiring presence would
    instead red every announce from a variant-shipping package the moment
    `ocx` stops writing the field — the field is being retired, and a gate
    that demands a vestigial key is the thing that would break.

    Calls `version_order.variant_names` — the same function `core/render.py`
    derives the catalog's value with, so the gate and the only remaining reader
    cannot disagree by construction. No writer computes it any more: neither
    `regenerate` nor `ocx package announce` records the field, so in practice
    this check now only ever sees a hand-authored one.
    """
    derived = variant_names(root.tags)
    if root.variants and root.variants != derived:
        raise ValidationError(
            f"variants {list(root.variants)} does not match the set derived from tags "
            f"{list(derived)} — the field is a projection of `tags`, not an independent claim"
        )


def check_tag_timestamps_z_anchored(root: PackageRoot) -> None:
    """Input-boundary guard (review-round-1 finding #3): every
    `tags[*].observed` and `tags[*].yanked.at` must match `_TIMESTAMP_RE`.

    The bot itself always writes this exact shape
    (`adapters/system_clock.py`); `yanked.at` is the one sub-field of
    `tags[*]` a human sets directly via PR (`schema/root.schema.json`'s
    `yanked` field, human-set, bot never writes it) and could otherwise
    supply a schema-legal `+02:00`-offset timestamp that silently breaks
    `core/render.py`'s `_generated_timestamp` lexicographic string max (that
    max is valid *only* when every candidate string shares this fixed,
    Z-anchored shape).
    """
    for tag_name, entry in root.tags.items():
        if _TIMESTAMP_RE.fullmatch(entry.observed) is None:
            raise ValidationError(
                f"tags[{tag_name}].observed {entry.observed!r} is not a "
                "Z-anchored UTC timestamp (YYYY-MM-DDThh:mm:ssZ)"
            )
        if entry.yanked is not None and _TIMESTAMP_RE.fullmatch(entry.yanked.at) is None:
            raise ValidationError(
                f"tags[{tag_name}].yanked.at {entry.yanked.at!r} is not a "
                "Z-anchored UTC timestamp (YYYY-MM-DDThh:mm:ssZ)"
            )


def check_namespace_not_reserved(package_id: PackageId, *, allow_reserved: bool = False) -> None:
    """ADR-2 ND-4: reject a reserved segment in either the namespace or the
    package position — a routing-collision guard, not a trademark denylist.

    `allow_reserved=True` narrows the blocked set to
    `RESERVED_NAMESPACE_SEGMENTS - RESERVED_BRAND_SEGMENTS` — an explicit,
    caller-opted-in carve-out for OCX's own first-party brand segments only
    (e.g. `ocx/cli`); control-path and generic segments are never admitted by
    this flag. Default `False` preserves ADR-2 ND-4's unconditional
    reservation.
    """
    blocked = (
        RESERVED_NAMESPACE_SEGMENTS - RESERVED_BRAND_SEGMENTS
        if allow_reserved
        else RESERVED_NAMESPACE_SEGMENTS
    )
    if package_id.namespace in blocked:
        raise ValidationError(f"namespace {package_id.namespace!r} is reserved (ADR-2 ND-4)")
    if package_id.package in blocked:
        raise ValidationError(f"package {package_id.package!r} is reserved (ADR-2 ND-4)")


_RESERVED_TAG_PREFIX: Final[str] = "__ocx"
"""Case-insensitive prefix OCX reserves for its own metadata tags on a
physical registry (`__ocx.desc` today). Reserved by *prefix*, not by exact
name — `__ocxfoo` is reserved too, so a future metadata tag never needs a
second governance decision."""

_CANONICAL_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"sha256\.[0-9a-fA-F]{64}|sha384\.[0-9a-fA-F]{96}|sha512\.[0-9a-fA-F]{128}"
)
"""`<algo>.<hex>` — the tag form `ocx package push` writes for every published
manifest so a digest reference is fetchable by tag. Hex is matched
case-insensitively even though OCX only ever emits lowercase: a hand-authored
PR is untrusted input, and the client's digest parser accepts either case.
The per-algorithm hex length is exact, so `sha384.<64 hex>` is *not* reserved
— a prefix-only check would over-reject a legitimate tag."""


def is_reserved_tag(tag: str) -> bool:
    """D7: is `tag` a name this index refuses to record?

    Two classes, both of which a physical registry legitimately carries and
    neither of which is package content: OCX's own `__ocx*` metadata tags,
    and the canonical `<algo>.<hex>` tags `ocx package push` writes.

    The one implementation of this rule. `check_no_reserved_tags` rejects a
    hand-authored root carrying such a tag; `core/observe.py`'s sweep imports
    this same predicate to exclude them instead of refusing the whole
    repository. Two copies of the rule would drift, and the drift would be
    invisible until a published package stopped reconciling.
    """
    return (
        tag.lower().startswith(_RESERVED_TAG_PREFIX) or _CANONICAL_TAG_RE.fullmatch(tag) is not None
    )


def check_no_reserved_tags(root: PackageRoot) -> None:
    """D7 at the index layer: no `tags` key may be a reserved tag name.

    This is the layer a hand-authored PR passes through, and the only one —
    `schema/root.schema.json`'s `propertyNames` documents the same intent but
    cannot express the full rule. Every offending key is listed, not just the
    first, so one PR round-trip fixes them all.
    """
    reserved = sorted(tag for tag in root.tags if is_reserved_tag(tag))
    if reserved:
        raise ValidationError(
            "reserved tag name(s) in tags: "
            + ", ".join(repr(tag) for tag in reserved)
            + f" — the {_RESERVED_TAG_PREFIX}* prefix and the canonical "
            "sha256./sha384./sha512.<hex> forms are reserved (D7)"
        )


def check_repository_allowlisted(repository: str, allowed_hosts: frozenset[str]) -> None:
    """G-03: `repository`'s host must be one of `allowed_hosts`.

    `allowed_hosts` is this deployment's committed registry-host policy
    (`core/policy.py`, `.github/index-policy.json`), loaded once by
    `cli/_wiring.py` and passed down — deliberately a required argument with
    no default rather than a constant compiled in here, so no caller can
    silently run G-03 against a policy nobody stated (OCX's index is one
    format, many copies; the public index's `ghcr.io` is not a corporate
    copy's Harbor/Artifactory host).

    Pure string parsing only (`urllib.parse`, no regex needed for the
    scheme/host split) — this function never touches a `RegistryPort`, so it
    is structurally impossible for it to make a network call. Callers must
    run this **before** any `RegistryPort` call (SSRF ordering, BD-1).
    """
    parsed = urlsplit(repository)
    if parsed.scheme != "oci" or not parsed.netloc:
        raise ValidationError(f"repository {repository!r} is not a valid oci://<host>/<path> URI")
    host = parsed.hostname
    if host is None or host not in allowed_hosts:
        raise ValidationError(
            f"repository host {host!r} is not on this index's registry-host allowlist "
            f"{sorted(allowed_hosts)} (G-03; policy: {INDEX_POLICY_PATH})"
        )


def check_repository_shape(repository: str) -> None:
    """Validate the `<path>` portion of `oci://<host>/<path>` against
    `OCI_REPOSITORY_RE` — the N-segment grammar, never `PACKAGE_ID_RE`.
    """
    parsed = urlsplit(repository)
    path = parsed.path.lstrip("/")
    if not path or OCI_REPOSITORY_RE.fullmatch(path) is None:
        raise ValidationError(f"repository path {path!r} does not match the OCI repository grammar")


def parse_digest(raw: str) -> str:
    """`re.fullmatch(r"sha256:[a-f0-9]{64}", raw)` or `ValidationError`.

    Every digest-shaped string anywhere in the bot is validated through this
    one function before it is ever used to build a filesystem path —
    digest-hex `fullmatch` before path join, no exceptions.
    """
    if _DIGEST_RE.fullmatch(raw) is None:
        raise ValidationError(f"{raw!r} is not a valid sha256 digest")
    return raw


def cas_relpath(namespace: str, package: str, digest: str, ext: str) -> str:
    """Deployed relative path (no leading `/`) of a CAS object.

    `p/<namespace>/<package>/o/sha256/<hex>.<ext>` per the wire path map
    (`plan_index_v1.md`). `digest` is the full `sha256:<hex>` string; only
    the hex half appears in the path itself.
    """
    hex_digest = digest.removeprefix("sha256:")
    return f"p/{namespace}/{package}/o/sha256/{hex_digest}.{ext}"


def check_digest_self_consistent(digest: str, object_bytes: bytes) -> None:
    """CAS integrity: `object_bytes` must hash to `digest` — the bytes as
    committed, which for a tag object are the registry's own (§1). Mismatch
    is `AnomalyError` — the file's name (or the field claiming this digest)
    lies about its own content, not a routine validation failure.

    Generalizes the original `TagEntry`-shaped check below to any claimed
    digest string (fork-PR announce revamp: `Desc.readme`/`Desc.logo` blobs
    need the identical self-consistency guarantee a tag's CAS object always
    got — closing a real gap where only tag digests were ever byte-verified,
    `core/verify_claims.py` and `cli/validate.py`'s blanket per-file scan).
    """
    computed = f"sha256:{hashlib.sha256(object_bytes).hexdigest()}"
    if computed != digest:
        raise AnomalyError(
            f"content digest self-consistency violated: claimed {digest!r}, "
            f"object bytes hash to {computed!r}"
        )


def check_content_digest_self_consistent(tag: TagEntry, object_bytes: bytes) -> None:
    """CAS integrity for one `TagEntry`: `object_bytes` must hash to
    `tag.content`. Thin wrapper over `check_digest_self_consistent` — kept
    for its existing callers/tests rather than churning every call site onto
    the more general signature."""
    check_digest_self_consistent(tag.content, object_bytes)


def check_no_dangling_references(root: PackageRoot, cas_digests: frozenset[str]) -> None:
    """Every `TagEntry.content` and `Desc.readme`/`Desc.logo` must appear in
    `cas_digests` (this package's `o/sha256/` tree, per `FilePort.list_files`).

    A root pointing at a CAS object that doesn't exist is corruption, not a
    routine PR mistake — `AnomalyError`, listing every dangling reference
    found (not just the first) so a human fixing the PR sees the whole
    picture in one pass.
    """
    missing: list[str] = []
    for tag_name, entry in root.tags.items():
        if entry.content not in cas_digests:
            missing.append(f"tags[{tag_name}].content -> {entry.content}")
    if root.desc is not None:
        if root.desc.readme is not None and root.desc.readme not in cas_digests:
            missing.append(f"desc.readme -> {root.desc.readme}")
        if root.desc.logo is not None and root.desc.logo not in cas_digests:
            missing.append(f"desc.logo -> {root.desc.logo}")
    if missing:
        raise AnomalyError("dangling CAS reference(s): " + "; ".join(missing))


# --- PackageRoot <-> dict codec (CONTRACTS.md §5.6) -------------------------


def _owner_to_dict(owner: Owner) -> dict[str, Any]:
    return {"github": owner.github, "github_id": owner.github_id}


def _owner_from_dict(data: dict[str, Any]) -> Owner:
    return Owner(github=data["github"], github_id=data["github_id"])


def _upstream_to_dict(upstream: Upstream) -> dict[str, Any]:
    data: dict[str, Any] = {"org": upstream.org}
    if upstream.repository_url is not None:
        data["repository_url"] = upstream.repository_url
    data["disclaimer"] = upstream.disclaimer  # schema allows null here (unlike repository_url)
    return data


def _upstream_from_dict(data: dict[str, Any]) -> Upstream:
    return Upstream(
        org=data["org"],
        repository_url=data.get("repository_url"),
        disclaimer=data.get("disclaimer"),
    )


def _desc_to_dict(desc: Desc) -> dict[str, Any]:
    data: dict[str, Any] = {
        "digest": desc.digest,
        "title": desc.title,
        "description": desc.description,
        "keywords": list(desc.keywords),
    }
    if desc.readme is not None:
        data["readme"] = desc.readme
    if desc.logo is not None:
        data["logo"] = desc.logo
    return data


def _desc_from_dict(data: dict[str, Any]) -> Desc:
    return Desc(
        digest=data["digest"],
        title=data["title"],
        description=data["description"],
        keywords=tuple(data.get("keywords", ())),
        readme=data.get("readme"),
        logo=data.get("logo"),
    )


def _yank_to_dict(yank: Yank) -> dict[str, Any]:
    return {"reason": yank.reason, "at": yank.at}


def _yank_from_dict(data: dict[str, Any]) -> Yank:
    return Yank(reason=data["reason"], at=data["at"])


def _tag_entry_to_dict(entry: TagEntry) -> dict[str, Any]:
    data: dict[str, Any] = {"content": entry.content, "observed": entry.observed}
    if entry.yanked is not None:
        data["yanked"] = _yank_to_dict(entry.yanked)
    return data


def _tag_entry_from_dict(data: dict[str, Any]) -> TagEntry:
    yanked_raw = data.get("yanked")
    yanked = None if yanked_raw is None else _yank_from_dict(yanked_raw)
    return TagEntry(content=data["content"], observed=data["observed"], yanked=yanked)


def serialize_package_root(root: PackageRoot) -> bytes:
    """The exact bytes committed to `p/<ns>/<pkg>.json` — pretty-printed,
    preserving `model.PackageRoot`'s declared field order (matching
    `schema/root.schema.json`'s `required` order once `upstream` is
    omitted), plus a trailing newline. The root is the one document in this
    index whose bytes this bot authors — CAS objects beside it are the
    registry's own bytes, copied. Optimized for PR review; the root's own
    bytes are never digested.
    """
    data: dict[str, Any] = {
        "name": root.name,
        "repository": root.repository,
        "owners": [_owner_to_dict(o) for o in root.owners],
        "status": root.status,
        "deprecated_message": root.deprecated_message,
        "created": root.created,
        "desc": None if root.desc is None else _desc_to_dict(root.desc),
    }
    if root.upstream is not None:
        data["upstream"] = _upstream_to_dict(root.upstream)
    if root.superseded_by is not None:
        data["superseded_by"] = root.superseded_by
    if root.source is not None:
        data["source"] = root.source
    if root.variants:
        data["variants"] = list(root.variants)
    data["tags"] = {tag: _tag_entry_to_dict(entry) for tag, entry in root.tags.items()}
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    return text.encode("utf-8")


def parse_package_root(raw: bytes) -> PackageRoot:
    """The `dict` <-> dataclass codec's read side.

    Raises `ValidationError` on any structurally malformed input (missing
    required key, wrong JSON type). Does not re-validate shape-schema
    concerns already covered by `check-jsonschema` (regex patterns, enum
    membership) — only needs to not crash on well-formed-but-unexpected JSON
    and to fail loudly (never partially construct a `PackageRoot`) on
    malformed JSON.
    """
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"malformed root JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("root JSON must be a JSON object")
    data = cast("dict[str, Any]", parsed)
    try:
        owners = tuple(_owner_from_dict(o) for o in data["owners"])
        desc_raw = data["desc"]
        desc = None if desc_raw is None else _desc_from_dict(desc_raw)
        upstream_raw = data.get("upstream")
        upstream = None if upstream_raw is None else _upstream_from_dict(upstream_raw)
        superseded_by = data.get("superseded_by")
        source = data.get("source")
        # An absent key and an empty array both mean "no variants"; the
        # serializer only ever emits the former, so `[]` on the wire
        # round-trips to an omitted key. That normalization is deliberate —
        # one spelling for one state — and is exactly what the byte gate
        # rejects a hand-authored `"variants": []` for.
        variants = tuple(data.get("variants") or ())
        tags = {name: _tag_entry_from_dict(t) for name, t in data["tags"].items()}
        return PackageRoot(
            name=data["name"],
            repository=data["repository"],
            owners=owners,
            status=data["status"],
            deprecated_message=data["deprecated_message"],
            created=data["created"],
            desc=desc,
            upstream=upstream,
            superseded_by=superseded_by,
            source=source,
            variants=variants,
            tags=tags,
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValidationError(f"malformed root structure: {exc}") from exc


# --- image-index CAS object ------------------------------------------------


def parse_image_index_digests(raw: bytes) -> tuple[str, ...]:
    """Every `manifests[*].digest` of one committed CAS object.

    Doubles as the document-kind gate (D4(c)): the bytes under a package's
    `o/sha256/` prefix are the registry's OCI image index, stored verbatim, so
    a CAS object that hashes correctly to its own filename but is not an image
    index is still a rejected root. Nothing here re-serializes — the returned
    digests are read out of the committed bytes, which stay untouched.

    Raises `ValidationError` on anything that is not an image index carrying a
    `manifests` list of objects with string `digest` fields. The digests
    themselves are *not* shape-checked here; the caller runs `parse_digest`
    before using one (digest-hex `fullmatch` before any path join or
    `RegistryPort` call).
    """
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"malformed CAS object JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("CAS object JSON must be a JSON object")
    manifests = cast("dict[str, Any]", parsed).get("manifests")
    if not isinstance(manifests, list):
        raise ValidationError(
            "CAS object is not an OCI image index (no `manifests` list); this index "
            "records image indices only"
        )
    digests: list[str] = []
    for descriptor in cast("list[object]", manifests):
        digest = (
            cast("dict[str, object]", descriptor).get("digest")
            if isinstance(descriptor, dict)
            else None
        )
        if not isinstance(digest, str):
            raise ValidationError("image-index descriptor has no string `digest`")
        digests.append(digest)
    return tuple(digests)
