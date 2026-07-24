"""Canonical index git-tree fixtures, byte-exact by construction.

`build_git_tree` materializes a `p/<ns>/<pkg>.json` package root plus its
package-local `o/sha256/**` CAS observation objects from a manifest spec,
using the REAL `serialize_package_root` / `serialize_observation_object`
serializers and the REAL `observe_one_tag` pipeline — so a seeded tree is
exactly what `indexbot validate` re-derives and byte-compares against, and a
CAS content digest written here equals the one the real registry adapter +
`observe` recompute when the paired `FakeGhcrServer` serves the same manifest
back.

The manifest-to-observation-object derivation reuses `core/observe.py` through
a tiny build-time `RegistryPort` (`_ManifestOracle`) that resolves the spec's
manifests as the exact canonical wire bytes `fake_ghcr` will serve — one
serialization, shared by seed time and serve time, so the two cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from indexbot.core.observe import observe_one_tag
from indexbot.core.validate_entry import (
    cas_relpath,
    parse_package_id,
    serialize_observation_object,
    serialize_package_root,
)
from indexbot.model import ManifestFetch, Owner, PackageRoot, Status, TagEntry
from tests.integration.harness.fake_ghcr import canonical_manifest_bytes, manifest_digest

if TYPE_CHECKING:
    from pathlib import Path

    from indexbot.model import OwnershipProbeResult

_DEFAULT_OWNERS: tuple[Owner, ...] = (Owner(github="indexbot-tester", github_id=1),)
_FIXED_TIMESTAMP = "2026-07-17T00:00:00Z"


@dataclass(frozen=True, slots=True)
class PackageSpec:
    """One package to seed: its physical `repository` and a `tags` map from tag
    name to the physical OCI manifest the registry serves for it (a bare
    single-platform manifest or a multi-platform image index — `observe`
    handles both)."""

    repository: str
    tags: Mapping[str, Mapping[str, object]]
    owners: tuple[Owner, ...] = _DEFAULT_OWNERS
    created: str = _FIXED_TIMESTAMP
    observed: str = _FIXED_TIMESTAMP
    status: Status = "active"


@dataclass(slots=True)
class _ManifestOracle:
    """Build-time `RegistryPort`: resolves a manifest spec to the exact
    canonical wire bytes `fake_ghcr` serves, so `observe_one_tag`'s computed
    content digest here matches validate time. Only `get_manifest` is reached
    (via `observe_one_tag`); the rest are never called in the seed path."""

    manifests: dict[tuple[str, str], Mapping[str, object]] = field(
        default_factory=dict[tuple[str, str], Mapping[str, object]]
    )

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        try:
            manifest = self.manifests[(repository, reference)]
        except KeyError:
            raise KeyError(f"no manifest for {repository}@{reference}") from None
        body = canonical_manifest_bytes(manifest)
        return ManifestFetch(raw=body, digest=manifest_digest(body), parsed=dict(manifest))

    def list_tags(self, repository: str) -> list[str]:
        raise NotImplementedError

    def get_desc_tag_digest(self, repository: str) -> str | None:
        raise NotImplementedError

    def get_blob(self, repository: str, digest: str) -> bytes:
        raise NotImplementedError

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        raise NotImplementedError


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_git_tree(root: Path, spec: Mapping[str, PackageSpec]) -> None:
    """Materialize every package in `spec` under `root` as a canonical index
    tree: `p/<ns>/<pkg>.json` (via `serialize_package_root`) plus one
    `p/<ns>/<pkg>/o/sha256/<hex>.json` CAS observation object per tag (via
    `observe_one_tag` + `serialize_observation_object`).

    `spec` keys are `<namespace>/<package>` ids (validated with the real
    `parse_package_id`); each value's `tags` map gives the physical manifest
    for each announced tag.
    """
    for package_id_str, package in spec.items():
        package_id = parse_package_id(package_id_str)
        oracle = _ManifestOracle(
            manifests={
                (package.repository, tag): manifest for tag, manifest in package.tags.items()
            }
        )
        tag_entries: dict[str, TagEntry] = {}
        for tag in package.tags:
            observation = observe_one_tag(package.repository, tag, oracle)
            if observation is None:
                raise ValueError(f"manifest for {package_id_str}@{tag} did not resolve")
            cas_bytes = serialize_observation_object(observation.object)
            relpath = cas_relpath(
                package_id.namespace, package_id.package, observation.content_digest, "json"
            )
            _write(root / relpath, cas_bytes)
            tag_entries[tag] = TagEntry(
                content=observation.content_digest, observed=package.observed
            )

        package_root = PackageRoot(
            name=f"ocx.sh/{package_id.namespace}/{package_id.package}",
            repository=package.repository,
            owners=package.owners,
            status=package.status,
            deprecated_message=None,
            created=package.created,
            desc=None,
            tags=tag_entries,
        )
        _write(
            root / f"p/{package_id.namespace}/{package_id.package}.json",
            serialize_package_root(package_root),
        )
