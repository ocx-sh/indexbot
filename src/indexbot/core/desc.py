"""`__ocx.desc` artifact handling (ADR-1 D6; CONTRACTS.md §7).

Ground truth ported from `ocx/crates/ocx_lib`'s `oci/client.rs::pull_description`
and `oci/annotations.rs`, not guessed:

- Tag name: literal `"__ocx.desc"`.
- Manifest: a single OCI image manifest (never an image index) with
  `artifactType == "application/vnd.sh.ocx.description.v1"`.
- `manifest.layers[]`: exactly one layer with `mediaType ==
  "application/markdown"` (the readme — required) and at most one layer
  with `mediaType` `"image/png"` or `"image/svg+xml"` (the logo — optional).
- `manifest.annotations` (manifest-level, not layer-level):
  `org.opencontainers.image.title`, `org.opencontainers.image.description`,
  `sh.ocx.keywords` (comma-separated, split/stripped/empty-dropped —
  matches `ocx/scripts/catalog-generate.py`'s `parse_keywords` exactly).
- Readme/logo bytes are copied verbatim — no frontmatter re-parsing (that
  machinery is publish-side only; the index bot only ever fetches).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from indexbot.core.validate_entry import parse_digest
from indexbot.model import Desc

if TYPE_CHECKING:
    from indexbot.ports import RegistryPort

_Manifest = dict[str, object]

_DESC_TAG: Final[str] = "__ocx.desc"
_README_MEDIA_TYPE: Final[str] = "application/markdown"
_LOGO_MEDIA_TYPES: Final[frozenset[str]] = frozenset({"image/png", "image/svg+xml"})
_TITLE_ANNOTATION: Final[str] = "org.opencontainers.image.title"
_DESCRIPTION_ANNOTATION: Final[str] = "org.opencontainers.image.description"
_KEYWORDS_ANNOTATION: Final[str] = "sh.ocx.keywords"


@dataclass(frozen=True, slots=True)
class DescUpdate:
    """Non-`None` return of `check_desc_change` — what the caller persists."""

    desc: Desc
    readme_bytes: bytes | None
    logo_bytes: bytes | None


def _last_segment(path: str) -> str:
    """The part after the last `/`, or the whole string when it holds none.
    A trailing slash yields the empty string, which the caller's chain skips."""
    return path.rsplit("/", 1)[-1]


def _title(annotations: _Manifest, name: str, repository: str) -> str:
    """The catalog title, never empty.

    `org.opencontainers.image.title` is optional on the publisher's side, but
    `schema/root.schema.json` gives `desc.title` `minLength: 1`. Defaulting an
    absent annotation to `""` therefore produces a root that passes every
    PR-time check and then fails `schema:validate:rendered` after merge —
    blocking the site deploy for *every* package, not just this one.

    The fallback chain matches `ocx`'s `announce::pipeline::title` exactly
    (annotation, then the last segment of the logical name, then of the
    physical repository). Byte-parity matters beyond tidiness: the two tools
    write the same root for the same registry state, and a title they disagree
    on would make each see the other's root as changed, so the C6
    unchanged-is-a-no-op short-circuit would flip-flop between them forever.
    """
    annotated = cast(str, annotations.get(_TITLE_ANNOTATION, ""))
    for candidate in (annotated, _last_segment(name)):
        if candidate:
            return candidate
    # Terminal, and always non-empty: `root.schema.json`'s `repository` pattern
    # ends in an alphanumeric path segment, so no trailing-slash input reaches
    # here. `ocx` carries a fourth rung (its `Physical::display`) that has no
    # counterpart on this side.
    return _last_segment(repository)


def _parse_keywords(raw: object) -> tuple[str, ...]:
    """Comma-separated `sh.ocx.keywords` -> stripped, empty-dropped tuple —
    matches `ocx/scripts/catalog-generate.py`'s `parse_keywords` exactly."""
    if not isinstance(raw, str) or not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _cas_digest(content: bytes) -> str:
    """This index's CAS address for `content` — sha256 over the exact bytes
    fetched, computed here rather than copied from the `__ocx.desc` layer
    descriptor's `digest` field. This index never adopts a digest it did not
    derive from content it holds (`ports.py`'s digest doctrine, D2/D5); that
    the two agree for a conforming registry is a check, not a shortcut."""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def check_desc_change(
    repository: str, current: Desc | None, registry: RegistryPort, *, name: str
) -> DescUpdate | None:
    """Compares `registry.get_desc_tag_digest(repository)` against
    `current.digest` (or `None` if `current is None`). Returns `None` (no
    change — caller keeps `current` verbatim, writes nothing new) if they
    match, including both-absent. Otherwise fetches the `__ocx.desc`
    manifest and its layers per the format above, builds the new `Desc`
    (`digest` = the observed `__ocx.desc` tag digest itself, not a
    recomputed content hash — this is a floating-tag comparison, D6, not a
    CAS digest) and returns a `DescUpdate` whose `readme_bytes`/
    `logo_bytes` the caller writes as this package's new CAS objects at
    `o/sha256/<hex>.<ext>` (`hex` = sha256 of those exact bytes per §1;
    `.md` for the readme, `.svg`/`.png` for the logo per its layer media
    type — `DescUpdate` itself carries no extension/media-type field, per
    CONTRACTS.md's frozen dataclass shape; see `open_questions`).
    `desc.readme`/`desc.logo` in the returned `Desc` are those same
    `sha256:<hex>` digest strings. A missing logo layer -> `logo_bytes =
    None`, `desc.logo = None`. A missing `sh.ocx.keywords` annotation ->
    `desc.keywords = ()`.

    `name` is the entry's logical name; it feeds `_title`'s fallback chain
    so an absent title annotation never yields the schema-illegal `""`.
    """
    observed_digest = registry.get_desc_tag_digest(repository)
    current_digest = current.digest if current is not None else None
    if observed_digest == current_digest:
        return None
    if observed_digest is None:
        # ponytail: __ocx.desc existed at current.digest and has since
        # disappeared from the registry — retraction semantics are
        # unspecified by ADR-1 D6 (open_questions). Raising loudly rather
        # than silently clearing `desc` back to null.
        raise ValueError(f"__ocx.desc tag disappeared from {repository!r} (was {current_digest!r})")

    manifest = registry.get_manifest(repository, _DESC_TAG).parsed
    annotations = cast(_Manifest, manifest.get("annotations") or {})
    title = _title(annotations, name, repository)
    description = cast(str, annotations.get(_DESCRIPTION_ANNOTATION, ""))
    keywords = _parse_keywords(annotations.get(_KEYWORDS_ANNOTATION))

    readme_bytes: bytes | None = None
    logo_bytes: bytes | None = None
    for layer in cast("list[_Manifest]", manifest.get("layers", [])):
        media_type = layer.get("mediaType")
        # digest-hex fullmatch before it ever reaches a RegistryPort call
        # (validate_entry.py's rule) — this layer digest is read verbatim
        # from a registry-fetched manifest that the entry's own repository
        # owner fully controls, not yet validated at this point.
        if media_type == _README_MEDIA_TYPE:
            readme_bytes = registry.get_blob(repository, parse_digest(cast(str, layer["digest"])))
        elif media_type in _LOGO_MEDIA_TYPES:
            logo_bytes = registry.get_blob(repository, parse_digest(cast(str, layer["digest"])))

    if readme_bytes is None:
        raise ValueError(f"__ocx.desc manifest for {repository!r} has no markdown readme layer")

    return DescUpdate(
        desc=Desc(
            digest=observed_digest,
            title=title,
            description=description,
            keywords=keywords,
            readme=_cas_digest(readme_bytes),
            logo=_cas_digest(logo_bytes) if logo_bytes is not None else None,
        ),
        readme_bytes=readme_bytes,
        logo_bytes=logo_bytes,
    )
