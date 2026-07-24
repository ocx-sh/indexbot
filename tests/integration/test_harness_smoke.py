"""Smoke proof: the harness drives the real bot end to end.

`test_validate_end_to_end` is the full loop the whole Track-B safety net is
built on — seed a canonical git tree, serve its one tag's manifest from the
socket-level `FakeGhcrServer`, run `indexbot validate <root>` through the real
`main()` (real argparse -> real `_wiring` -> real `GhcrRegistry` adapter -> real
`LocalFiles` adapter), and assert a clean `ExitCode.OK`. It confirms the real
anonymous bearer-token dance ran against the fake, not a re-mock.

`test_fake_forge_drives_github_api` is the paired proof for the forge fake: the
real `GitHubApi` adapter, pointed at `FakeForgeServer`, round-trips reads and a
PR-open over a real socket.
"""

from __future__ import annotations

import base64
import functools
from pathlib import Path

import pytest

from indexbot.adapters.ghcr import GhcrRegistry
from indexbot.adapters.github_api import GitHubApi
from indexbot.cli.main import main
from indexbot.exit_codes import ExitCode
from tests.integration.fixtures.canonical import (
    CANONICAL_MANIFEST,
    CANONICAL_REPO_PATH,
    CANONICAL_ROOT_PATH,
    CANONICAL_SPEC,
    CANONICAL_TAG,
)
from tests.integration.harness.fake_forge import FakeForgeServer
from tests.integration.harness.fake_ghcr import FakeGhcrServer
from tests.integration.harness.git_tree import build_git_tree

_WIRING_GHCR = "indexbot.cli._wiring.GhcrRegistry"


def test_validate_end_to_end(
    fake_ghcr: FakeGhcrServer, index_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_git_tree(index_tree, CANONICAL_SPEC)
    fake_ghcr.add_manifest(CANONICAL_REPO_PATH, CANONICAL_TAG, CANONICAL_MANIFEST)

    # Seam 1 (FilePort root): `_wiring._repo_root()` reads GITHUB_WORKSPACE.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(index_tree))
    # Seam 2 (RegistryPort base URL): `_wiring._run_validate` builds a bare
    # `GhcrRegistry()`; thread the fake's base URL through the adapter's own
    # `base_url` constructor arg by monkeypatching the wiring's class reference.
    monkeypatch.setattr(_WIRING_GHCR, functools.partial(GhcrRegistry, base_url=fake_ghcr.base_url))

    exit_code = main(["validate", CANONICAL_ROOT_PATH])

    assert exit_code == ExitCode.OK
    served = fake_ghcr.requests
    # The real anonymous bearer-token dance ran against the fake (a first
    # unauthenticated /v2/ request 401'd, the adapter fetched a pull token).
    assert ("GET", "/token") in served
    # The tag manifest and the per-platform digest scope check both hit the fake.
    assert any(path.endswith(f"/manifests/{CANONICAL_TAG}") for _method, path in served)
    assert any("/manifests/sha256:" in path for _method, path in served)


def test_fake_forge_drives_github_api(fake_forge: FakeForgeServer) -> None:
    encoded = base64.b64encode(b'{"format_version":1}').decode("ascii")
    fake_forge.stub_json(
        "GET", fake_forge.repo_path("git", "ref", "heads", "main"), {"object": {"sha": "abc123"}}
    )
    fake_forge.stub_json(
        "GET",
        fake_forge.repo_path("contents", "config.json"),
        {"content": encoded, "encoding": "base64"},
        params={"ref": "main"},
    )
    fake_forge.stub_json(
        "GET",
        fake_forge.repo_path("pulls"),
        [],
        params={"head": "ocx-sh:announce-acme-widget", "base": "main", "state": "open"},
    )
    fake_forge.stub_json("POST", fake_forge.repo_path("pulls"), {"number": 42}, status=201)

    api = GitHubApi(
        owner="ocx-sh",
        repo="index",
        token="fake-token",  # noqa: S106 - test fixture, not a real credential
        base_url=fake_forge.base_url,
        graphql_url=f"{fake_forge.base_url}{fake_forge.graphql_path()}",
    )

    assert api.get_ref_sha("main") == "abc123"
    assert api.get_file_contents("config.json", "main") == b'{"format_version":1}'
    assert (
        api.open_or_update_pull_request(
            branch="announce-acme-widget", base="main", title="regen acme/widget", body="body"
        )
        == 42
    )
