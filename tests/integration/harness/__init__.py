"""Socket-level test doubles and canonical git-tree builder.

`FakeGhcrServer` / `FakeForgeServer` are stdlib `http.server` fakes that drive
the REAL `adapters/registry_v2.py` / `adapters/github_api.py` over a real socket;
`build_git_tree` seeds a canonical `p/` tree (the root byte-exact against the
real serializer, each CAS object the registry's own image-index bytes).
`ScriptedResponse` / `json_response` script individual routes.
"""

from __future__ import annotations

from tests.integration.harness._http import ScriptedResponse, json_response
from tests.integration.harness.fake_forge import FakeForgeServer
from tests.integration.harness.fake_ghcr import (
    FakeGhcrServer,
    manifest_digest,
    manifest_wire_bytes,
)
from tests.integration.harness.git_tree import PackageSpec, build_git_tree

__all__ = [
    "FakeForgeServer",
    "FakeGhcrServer",
    "PackageSpec",
    "ScriptedResponse",
    "build_git_tree",
    "json_response",
    "manifest_digest",
    "manifest_wire_bytes",
]
