"""Schema-adjacent semantic checks on a committed `PackageRoot` / `ObservationObject`.

Everything JSON Schema *can* express (`schema/root.schema.json`,
`schema/observation-object.schema.json`) runs via `check-jsonschema`, never
imported here (ADR-4 BD-1). This module owns the checks a schema cannot
express: path<->name derivation (G-02), repository host allowlisting (G-03,
checked before any network intent — SSRF ordering), reserved-namespace
rejection (ADR-2 ND-4), digest-hex `fullmatch` before any path join,
content-digest self-consistency (CAS integrity), dangling-reference
detection, and the `PackageRoot`/`ObservationObject` <-> `dict` codec every
other module reuses (CONTRACTS.md §1/§5.6) rather than hand-rolling a second
encoder.

`OCI_REPOSITORY_RE` here and `core/validate_payload.py`'s `PACKAGE_ID_RE` are
two structurally distinct constants (ADR-4 BD-4's two-regex rule) — one
governs the physical, N-segment OCI repository grammar; the other governs the
logical, fixed-two-segment package id. Never shared, never guessed at
runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final, cast
from urllib.parse import urlsplit

from indexbot.core.validate_payload import parse_package_id
from indexbot.errors import AnomalyError, ValidationError
from indexbot.model import (
    Desc,
    ObservationObject,
    OciPlatform,
    Owner,
    PackageId,
    PackageRoot,
    PlatformEntry,
    TagEntry,
    Upstream,
    Yank,
)

# --- N-segment OCI repository grammar (BD-4's two-regex rule: never shared
# with core/validate_payload.py's fixed-two-segment PACKAGE_ID_RE). ---------
_COMPONENT = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
OCI_REPOSITORY_RE: Final[re.Pattern[str]] = re.compile(rf"^{_COMPONENT}(?:/{_COMPONENT})*$")

REPOSITORY_HOST_ALLOWLIST: Final[frozenset[str]] = frozenset({"ghcr.io"})
"""Anti-squat/anti-exfil guard (G-03). Extend only via reviewed PR."""

_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[a-f0-9]{64}")

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
    `<namespace>/<package>` id (reused via `validate_payload.parse_package_id`
    — never a second hand-rolled regex, ADR-4 BD-4) that does not name
    `root` itself.

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


def check_repository_allowlisted(repository: str) -> None:
    """G-03: `repository`'s host must be on `REPOSITORY_HOST_ALLOWLIST`.

    Pure string parsing only (`urllib.parse`, no regex needed for the
    scheme/host split) — this function never touches a `RegistryPort`, so it
    is structurally impossible for it to make a network call. Callers must
    run this **before** any `RegistryPort` call (SSRF ordering, BD-1).
    """
    parsed = urlsplit(repository)
    if parsed.scheme != "oci" or not parsed.netloc:
        raise ValidationError(f"repository {repository!r} is not a valid oci://<host>/<path> URI")
    host = parsed.hostname
    if host is None or host not in REPOSITORY_HOST_ALLOWLIST:
        raise ValidationError(f"repository host {host!r} is not allowlisted (G-03)")


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


def check_content_digest_self_consistent(tag: TagEntry, object_bytes: bytes) -> None:
    """CAS integrity: `object_bytes` (already serialized canonically, §1)
    must hash to `tag.content`. Mismatch is `AnomalyError` — the file's name
    lies about its own content, not a routine validation failure.
    """
    computed = f"sha256:{hashlib.sha256(object_bytes).hexdigest()}"
    if computed != tag.content:
        raise AnomalyError(
            f"content digest self-consistency violated: tag claims {tag.content!r}, "
            f"object bytes hash to {computed!r}"
        )


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
    omitted), plus a trailing newline. This is **not** §1's canonical
    minified form — that form is reserved for content-addressed CAS objects,
    which must dedup; the human-diffable root is optimized for PR review, and
    the root's own bytes are never digested (only referenced indirectly via
    `TagEntry.content`, which points at an `ObservationObject`).
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
            tags=tags,
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValidationError(f"malformed root structure: {exc}") from exc


# --- ObservationObject <-> dict codec (§1's canonical minified form) -------


def _platform_to_dict(platform: OciPlatform) -> dict[str, Any]:
    data: dict[str, Any] = {"architecture": platform.architecture, "os": platform.os}
    if platform.os_version is not None:
        data["os.version"] = platform.os_version
    if platform.os_features:
        data["os.features"] = list(platform.os_features)
    if platform.variant is not None:
        data["variant"] = platform.variant
    if platform.features:
        data["features"] = list(platform.features)
    return data


def _platform_from_dict(data: dict[str, Any]) -> OciPlatform:
    return OciPlatform(
        architecture=data["architecture"],
        os=data["os"],
        os_version=data.get("os.version"),
        os_features=tuple(data.get("os.features", ())),
        variant=data.get("variant"),
        features=tuple(data.get("features", ())),
    )


def _platform_entry_to_dict(entry: PlatformEntry) -> dict[str, Any]:
    return {"platform": _platform_to_dict(entry.platform), "digest": entry.digest}


def _platform_entry_from_dict(data: dict[str, Any]) -> PlatformEntry:
    return PlatformEntry(platform=_platform_from_dict(data["platform"]), digest=data["digest"])


def _platform_sort_key(entry: PlatformEntry) -> tuple[str, str, str, str]:
    platform = entry.platform
    return (platform.architecture, platform.os, platform.os_version or "", platform.variant or "")


def serialize_observation_object(obj: ObservationObject) -> bytes:
    """§1's canonical minified form — the CAS-digested encoding.

    `platforms` is sorted by `(architecture, os, os_version or "", variant or
    "")` before serialization so registry-returned manifest-list ordering
    never affects the digest, then dumped as
    `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
    """
    sorted_platforms = sorted(obj.platforms, key=_platform_sort_key)
    data = {"platforms": [_platform_entry_to_dict(e) for e in sorted_platforms]}
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("utf-8")


def parse_observation_object(raw: bytes) -> ObservationObject:
    """The `ObservationObject` codec's read side — same failure contract as
    `parse_package_root`."""
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"malformed observation object JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("observation object JSON must be a JSON object")
    data = cast("dict[str, Any]", parsed)
    try:
        platforms = tuple(_platform_entry_from_dict(p) for p in data["platforms"])
        return ObservationObject(platforms=platforms)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValidationError(f"malformed observation object structure: {exc}") from exc
