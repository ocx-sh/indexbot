"""Socket-level test doubles and canonical git-tree builder.

`FakeGhcrServer` / `FakeForgeServer` are stdlib `http.server` fakes that drive
the REAL `adapters/ghcr.py` / `adapters/github_api.py` over a real socket;
`build_git_tree` seeds a canonical `p/` tree (byte-exact against the real
serializers). `ScriptedResponse` / `json_response` script individual routes.
"""

from __future__ import annotations

from tests.integration.harness._http import ScriptedResponse, json_response
from tests.integration.harness.fake_forge import FakeForgeServer
from tests.integration.harness.fake_ghcr import (
    FakeGhcrServer,
    canonical_manifest_bytes,
    manifest_digest,
)
from tests.integration.harness.git_tree import PackageSpec, build_git_tree

__all__ = [
    "FakeForgeServer",
    "FakeGhcrServer",
    "PackageSpec",
    "ScriptedResponse",
    "build_git_tree",
    "canonical_manifest_bytes",
    "json_response",
    "manifest_digest",
]
