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


def _parse_keywords(raw: object) -> tuple[str, ...]:
    """Comma-separated `sh.ocx.keywords` -> stripped, empty-dropped tuple —
    matches `ocx/scripts/catalog-generate.py`'s `parse_keywords` exactly."""
    if not isinstance(raw, str) or not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _cas_digest(content: bytes) -> str:
    """This index's own CAS digest of `content` — deliberately independent
    of the registry's blob digest for that same content (a different digest
    namespace, mirroring `TagEntry.content` vs `PlatformEntry.digest`,
    D2/D5)."""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def check_desc_change(
    repository: str, current: Desc | None, registry: RegistryPort
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
    title = cast(str, annotations.get(_TITLE_ANNOTATION, ""))
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
