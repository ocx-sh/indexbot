"""One canonical package the integration flows seed and serve.

`acme/widget` at `oci://ghcr.io/ocx-contrib/widget`, one tag
`1.0.0_20260728` resolving to a single-platform (`linux/amd64`) OCI **image
index** — the only document kind this index records (ADR D4(a)).

The tag carries a build fragment deliberately: that is the shape
`core/anomaly.py` anomaly-checks (`core/version_order.is_build_pinned_version`),
so every flow seeded from this fixture exercises the tamper-detection path
rather than skipping it. `CANONICAL_ROLLING_TAG` is the cascade alias the same
publish repoints — the exempt shape — for flows that need to contrast the two.

`CANONICAL_SPEC` seeds the git tree; `CANONICAL_REPO_PATH` + `CANONICAL_TAG` +
`CANONICAL_INDEX` script the paired `FakeGhcrServer`; both derive the same CAS
content digest by construction.

`CANONICAL_LEAF` is the image manifest the index's one descriptor points at.
`cli/validate.py` re-fetches every `manifests[*].digest` through
`check_digest_in_scope`, so a flow that seeds this package must serve the leaf
as well — `seed_registry` does both in one call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.integration.harness.fake_ghcr import manifest_digest, manifest_wire_bytes
from tests.integration.harness.git_tree import PackageSpec

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tests.integration.harness.fake_ghcr import FakeGhcrServer

CANONICAL_PACKAGE_ID = "acme/widget"
CANONICAL_REPO_PATH = "ocx-contrib/widget"
CANONICAL_REPOSITORY = f"oci://ghcr.io/{CANONICAL_REPO_PATH}"
CANONICAL_TAG = "1.0.0_20260728"
CANONICAL_ROLLING_TAG = "1.0.0"
CANONICAL_ROOT_PATH = "p/acme/widget.json"

CANONICAL_LEAF: dict[str, object] = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": f"sha256:{'0' * 64}",
        "size": 0,
    },
    "layers": [],
}

LEAF_BYTES = manifest_wire_bytes(CANONICAL_LEAF)
LEAF_DIGEST = manifest_digest(LEAF_BYTES)

CANONICAL_INDEX: dict[str, object] = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": LEAF_DIGEST,
            "size": len(LEAF_BYTES),
            "platform": {"architecture": "amd64", "os": "linux"},
        }
    ],
}

CANONICAL_SPEC: dict[str, PackageSpec] = {
    CANONICAL_PACKAGE_ID: PackageSpec(
        repository=CANONICAL_REPOSITORY, tags={CANONICAL_TAG: CANONICAL_INDEX}
    )
}

ROLLING_SPEC: dict[str, PackageSpec] = {
    CANONICAL_PACKAGE_ID: PackageSpec(
        repository=CANONICAL_REPOSITORY, tags={CANONICAL_ROLLING_TAG: CANONICAL_INDEX}
    )
}
"""`CANONICAL_SPEC` with the cascade alias in place of the build-pinned tag.

Serving a different image index for this one is a *correct* publish, not
tamper — the contrast case a flow needs to prove the sweep does not alarm on
one."""


def seed_registry(
    ghcr: FakeGhcrServer,
    index: Mapping[str, object] = CANONICAL_INDEX,
    tag: str = CANONICAL_TAG,
) -> None:
    """Serve `index` at `tag`, plus `CANONICAL_LEAF` at its own digest so
    `check_digest_in_scope` resolves the index's descriptor. Every fixture
    index in this suite points at that one leaf."""
    ghcr.add_manifest(CANONICAL_REPO_PATH, tag, index)
    ghcr.add_manifest(CANONICAL_REPO_PATH, LEAF_DIGEST, CANONICAL_LEAF)
