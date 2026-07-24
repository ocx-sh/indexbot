"""One canonical package the smoke test seeds and serves.

`acme/widget` at `oci://ghcr.io/ocx-contrib/widget`, one tag `1.0.0` backed by
a bare single-platform (`linux/amd64`) OCI manifest. `CANONICAL_SPEC` seeds the
git tree; `CANONICAL_REPO_PATH` + `CANONICAL_TAG` + `CANONICAL_MANIFEST` script
the paired `FakeGhcrServer`; both derive the same CAS content digest by
construction.
"""

from __future__ import annotations

from tests.integration.harness.git_tree import PackageSpec

CANONICAL_PACKAGE_ID = "acme/widget"
CANONICAL_REPO_PATH = "ocx-contrib/widget"
CANONICAL_REPOSITORY = f"oci://ghcr.io/{CANONICAL_REPO_PATH}"
CANONICAL_TAG = "1.0.0"
CANONICAL_ROOT_PATH = "p/acme/widget.json"

CANONICAL_MANIFEST: dict[str, object] = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": f"sha256:{'0' * 64}",
        "size": 0,
        "platform": {"architecture": "amd64", "os": "linux"},
    },
    "layers": [],
}

CANONICAL_SPEC: dict[str, PackageSpec] = {
    CANONICAL_PACKAGE_ID: PackageSpec(
        repository=CANONICAL_REPOSITORY, tags={CANONICAL_TAG: CANONICAL_MANIFEST}
    )
}
