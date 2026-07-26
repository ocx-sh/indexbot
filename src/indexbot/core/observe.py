"""Registry-state observation (ADR-1 D1/D5; CONTRACTS.md §7).

Pure given its `RegistryPort` argument (CONTRACTS.md §0) — every physical
registry read goes through the injected port, never `httpx` directly.

This module copies bytes, it never projects them: `observe_one_tag` records
`ManifestFetch.raw` verbatim and `ManifestFetch.digest` as-is, so the digest
this index commits for a tag is the physical registry's own digest for the
OCI image index it served. There is no encoder between the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from indexbot.core.validate_entry import is_reserved_tag
from indexbot.errors import ValidationError

if TYPE_CHECKING:
    from indexbot.ports import RegistryPort

_Manifest = dict[str, object]

_MAX_INDEX_BYTES: Final[int] = 4 * 1024 * 1024
"""Ceiling on the bytes one tag may commit into this index.

Storing the registry's index verbatim (§1) moves the trust boundary: the
committed object's size is now whatever the publisher's registry chose to
serve, and an image index carries unbounded `annotations`. Without a bound,
one padded push lands permanently in the git repository and is served from
`p/<ns>/<pkg>/o/sha256/<hex>.json` — `check_digest_self_consistent` and the
index-shape check both pass, because the bytes are hash-correct and are an
image index; they are just enormous.

4 MiB is the OCI distribution spec's manifest size registries must accept,
not an invented number — anything above it is outside what a conforming
registry is obliged to serve in the first place."""

_SOURCE_ANNOTATION: Final[str] = "org.opencontainers.image.source"
"""Manifest-level annotation carrying the repository whose CI produced the
build (`ocx package push --annotation`). Read the same way `core/desc.py`
reads `__ocx.desc`'s title/keywords annotations, and `adapters/registry_v2.py`'s
`_embedded_identifier` reads `sh.ocx.name`."""

_SOURCE_SCHEME: Final[str] = "https://"
"""Only scheme accepted from `_SOURCE_ANNOTATION`. The value is
publisher-controlled and ends up as an `href` on a public page
(`site/.vitepress/theme/components/detail/MetaRail.vue`), so anything else —
`javascript:`, `data:`, plain `http:` — is dropped at ingestion rather than
carried into a root that `schema/root.schema.json`'s `source` pattern would
then reject at CI time. Mirrors that pattern exactly; keep the two in step."""


@dataclass(frozen=True, slots=True)
class Observation:
    """A record of what a tag resolved to at observation time.

    `raw` is the registry's OCI image index, verbatim — this class names the
    *event*, never the artifact.

    `content_digest` is `ManifestFetch.digest`: computed by the adapter from
    `raw` itself (`ports.py`'s digest doctrine), never trusted from a
    response header.

    `source` is this tag's manifest-level `org.opencontainers.image.source`
    annotation, or `None` when absent, non-string, or not `https://`-scheme
    (see `_SOURCE_SCHEME`). It rides on the observation because
    `core/regenerate.py` folds it into the package *root*, a root-level
    field, not a per-tag one. It is no cheaper to carry than any other
    annotation: annotations live inside the bytes being digested, so a
    publisher re-annotating an index moves `content_digest` with it.
    """

    tag: str
    content_digest: str
    raw: bytes
    source: str | None = None


def _source_annotation(raw: _Manifest) -> str | None:
    """`_SOURCE_ANNOTATION`'s value from a fetched manifest, or `None`.

    Same read shape as `adapters/registry_v2.py`'s `_embedded_identifier` (a
    non-string annotation value is a malformed manifest, treated as absent),
    plus the `_SOURCE_SCHEME` filter — the one place registry-controlled URL
    text enters this index, so it is also the one place to reject it.
    """
    annotations = raw.get("annotations")
    if not isinstance(annotations, dict):
        return None
    value = cast("dict[str, object]", annotations).get(_SOURCE_ANNOTATION)
    if not isinstance(value, str) or not value.startswith(_SOURCE_SCHEME):
        return None
    return value


def observe_one_tag(repository: str, tag: str, registry: RegistryPort) -> Observation | None:
    """One tag's freshly observed state, or `None` if `tag` no longer exists
    on `repository` (a real 404 — fetched but vanished, or never existed at
    all).

    `registry.get_manifest(repository, tag)` must resolve to an OCI image
    index — discriminated by a `"manifests"` key, the same untagged-shape
    test the OCX client makes on the wire. Anything else raises
    `ValidationError` (D4(a)): this index records image indices only, so a
    tag pointing at a single image manifest is a publishing fault to surface,
    not a shape to convert into a stand-in. Over `_MAX_INDEX_BYTES` of wire
    bytes is refused the same way — verbatim storage means the registry
    decides how much this repository commits, so the bound lives here, at the
    one point registry bytes enter.

    The returned `Observation` carries `ManifestFetch.raw` unmodified and
    `ManifestFetch.digest` as its `content_digest`.

    Extracted from `observe()`'s per-tag body (fork-PR announce revamp) so
    `core/verify_claims.py` (re-derive one *claimed* tag from registry truth)
    and `cli/announce.py` (observe only the publisher's curated tag set) can
    reuse this exact per-tag logic without looping over
    `registry.list_tags()` first — `observe()` below is a thin loop over
    this function.
    """
    try:
        fetch = registry.get_manifest(repository, tag)
    except KeyError:
        return None
    if len(fetch.raw) > _MAX_INDEX_BYTES:
        raise ValidationError(
            f"tag {tag!r} on {repository!r} serves {len(fetch.raw)} bytes, over the "
            f"{_MAX_INDEX_BYTES}-byte ceiling this index commits for one tag"
        )
    if "manifests" not in fetch.parsed:
        raise ValidationError(
            f"tag {tag!r} on {repository!r} resolves to an OCI image manifest, not an "
            "image index; the index records image indices only"
        )
    return Observation(
        tag=tag,
        content_digest=fetch.digest,
        raw=fetch.raw,
        source=_source_annotation(fetch.parsed),
    )


def observe(repository: str, registry: RegistryPort) -> tuple[Observation, ...]:
    """One `Observation` per `registry.list_tags(repository)` entry, via
    `observe_one_tag`, skipping every reserved tag name
    (`validate_entry.is_reserved_tag` — imported, never restated here).

    The exclusion is load-bearing, not tidiness. `ocx package push` writes a
    canonical `sha256.<hex>` tag beside every version tag, plus the
    `__ocx.desc` description tag; both resolve to bare image manifests.
    Sweeping them would make `observe_one_tag` refuse every ocx-published
    repository on the first reserved tag it met.

    A tag whose manifest fetch raises `KeyError` (fetched but vanished
    between `list_tags` and `get_manifest` — a real registry race) is
    skipped, not fatal — `observe_one_tag` returning `None`. A
    `TransientError` from either call propagates uncaught (BD-2 — the whole
    call fails transient, no partial-tag silent-skip).
    """
    observations: list[Observation] = []
    for tag in registry.list_tags(repository):
        if is_reserved_tag(tag):
            continue
        observation = observe_one_tag(repository, tag, registry)
        if observation is not None:
            observations.append(observation)
    return tuple(observations)
