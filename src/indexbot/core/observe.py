"""Registry-state observation (ADR-1 D1/D4/D5; CONTRACTS.md §7).

Pure given its `RegistryPort` argument (CONTRACTS.md §0) — every physical
registry read goes through the injected port, never `httpx` directly.
Content-digest computation reuses `core/validate_entry.py`'s canonical
`ObservationObject` encoder and `platform_sort_key` (CONTRACTS.md §1) rather
than a second copy of either, so two independently-computed digests for
identical content stay byte-equal (CAS dedup, ADR-1 D3).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from indexbot.core.validate_entry import platform_sort_key, serialize_observation_object
from indexbot.model import ObservationObject, OciPlatform, PlatformEntry

if TYPE_CHECKING:
    from indexbot.ports import RegistryPort

_Manifest = dict[str, object]

_DESC_TAG: Final[str] = "__ocx.desc"
"""The internal description tag — never a package-content tag. `list_tags`
returns it like any other tag on a real registry (`core/desc.py` reads it
through a dedicated `get_desc_tag_digest`/`get_manifest`-by-digest path, not
through this per-tag walk), so `observe()` must exclude it explicitly or it
would be misparsed as a malformed platform manifest. **Not stated by
CONTRACTS.md's `observe()` docstring — flagged in open_questions rather than
silently assumed.**"""


@dataclass(frozen=True, slots=True)
class Observation:
    """One tag's freshly observed state. Input to regenerate/anomaly."""

    tag: str
    content_digest: str
    object: ObservationObject


def _parse_platform(raw: _Manifest) -> OciPlatform:
    """Registry-side OCI `Platform` object -> domain type.

    A distinct parser from `validate_entry`'s private wire-format helpers:
    this reads a *physical registry's* raw manifest JSON, not this index's
    own CAS JSON — even though the OCI image-spec field shapes happen to
    coincide (both are the same inline `Platform` object subset, ADR-1 D4).
    Registry data is trusted here, not re-validated as an untrusted-input
    boundary (that discipline is scoped to the package-id/repository
    regexes, ADR-4 BD-4) — a malformed field raises `KeyError`/`TypeError`
    uncaught, a genuine bug per `cli/main.py`'s contract.
    """
    return OciPlatform(
        architecture=cast(str, raw["architecture"]),
        os=cast(str, raw["os"]),
        os_version=cast("str | None", raw.get("os.version")),
        os_features=tuple(cast("list[str]", raw.get("os.features", ()))),
        variant=cast("str | None", raw.get("variant")),
        features=tuple(cast("list[str]", raw.get("features", ()))),
    )


def _resolve_bare_platform(raw: _Manifest) -> _Manifest:
    """Locate platform info on a bare (non-index) manifest: its own
    `platform` field, or `config.platform` (CONTRACTS.md §7). Raises
    `ValueError` if neither is present — an image manifest with no platform
    info anywhere is not a shape this bot has a defined recovery for.
    """
    platform_raw = raw.get("platform")
    if platform_raw is None:
        config = cast("_Manifest | None", raw.get("config"))
        platform_raw = config.get("platform") if config is not None else None
    if platform_raw is None:
        raise ValueError("bare manifest has no platform/config.platform field")
    return cast(_Manifest, platform_raw)


def _platforms_from_index(raw: _Manifest) -> tuple[PlatformEntry, ...]:
    manifests = cast("list[_Manifest]", raw["manifests"])
    return tuple(
        PlatformEntry(
            platform=_parse_platform(cast(_Manifest, entry["platform"])),
            digest=cast(str, entry["digest"]),
        )
        for entry in manifests
    )


def _platforms_from_bare(raw: _Manifest, digest: str) -> tuple[PlatformEntry, ...]:
    """`digest` is `RegistryPort.get_manifest`'s adapter-computed
    `ManifestFetch.digest` for this same manifest fetch — the physical
    registry's real, content-derived digest (ADR-1 D5's verifiability
    chain), never a locally recomputed stand-in (see `ports.py`'s digest
    doctrine)."""
    platform_raw = _resolve_bare_platform(raw)
    return (PlatformEntry(platform=_parse_platform(platform_raw), digest=digest),)


def _content_digest(obj: ObservationObject) -> str:
    return f"sha256:{hashlib.sha256(serialize_observation_object(obj)).hexdigest()}"


def observe(repository: str, registry: RegistryPort) -> tuple[Observation, ...]:
    """One `Observation` per `registry.list_tags(repository)` entry
    (excluding the internal `__ocx.desc` tag, see `_DESC_TAG`).

    For each tag, `registry.get_manifest(repository, tag)` returns a
    `ManifestFetch` whose `.parsed` body is either an OCI image index
    (multi-platform, distinguished by a `"manifests"` key) or a bare image
    manifest (single platform, its own `platform`/`config.platform` field).
    A bare manifest's one `PlatformEntry.digest` is `ManifestFetch.digest`
    itself — the adapter-computed, content-derived registry digest (ADR-1
    D5's verifiability chain; `ports.py`'s digest doctrine), never a locally
    synthesized stand-in. `platforms[]` is sorted and digested per §1. A tag
    whose manifest fetch raises `KeyError` (fetched but vanished between
    `list_tags` and `get_manifest` — a real registry race) is skipped, not
    fatal. A `TransientError` from either call propagates uncaught (BD-2 —
    the whole call fails transient, no partial-tag silent-skip).
    """
    observations: list[Observation] = []
    for tag in registry.list_tags(repository):
        if tag == _DESC_TAG:
            continue
        try:
            fetch = registry.get_manifest(repository, tag)
        except KeyError:
            continue
        raw = fetch.parsed
        platforms = (
            _platforms_from_index(raw)
            if "manifests" in raw
            else _platforms_from_bare(raw, fetch.digest)
        )
        obj = ObservationObject(platforms=tuple(sorted(platforms, key=platform_sort_key)))
        observations.append(Observation(tag=tag, content_digest=_content_digest(obj), object=obj))
    return tuple(observations)
