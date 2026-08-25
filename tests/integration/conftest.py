"""Integration-harness fixtures: the two socket fakes and a blank index root.

`fake_ghcr` / `fake_forge` yield running, context-managed servers (torn down
after the test). `index_tree` is a fresh, empty checkout root for
`build_git_tree` to seed — orthogonal to the servers so a test composes them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from ocx_indexbot.core.policy import INDEX_POLICY_PATH
from tests.integration.harness.fake_forge import FakeForgeServer
from tests.integration.harness.fake_ghcr import FakeGhcrServer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def fake_ghcr() -> Iterator[FakeGhcrServer]:
    with FakeGhcrServer() as server:
        yield server


@pytest.fixture
def fake_forge() -> Iterator[FakeForgeServer]:
    with FakeForgeServer() as server:
        yield server


def write_policy(root: Path, registry_hosts: list[object]) -> None:
    """Seed an index checkout's `.github/index-policy.json`.

    The registry entries are the interesting part: `cli/_wiring.py` builds one
    client per entry, so pointing `ghcr.io`'s `base_url` at the loopback fake
    is how an integration flow reaches the fake at all — the same field a
    corporate deployment uses to name its own registry, exercised rather than
    monkeypatched.
    """
    policy = root / INDEX_POLICY_PATH
    policy.parent.mkdir(parents=True, exist_ok=True)
    document = {"name": "ocx.sh", "name_segments": 2, "registry_hosts": registry_hosts}
    policy.write_bytes(json.dumps(document).encode() + b"\n")


@pytest.fixture
def index_tree(tmp_path: Path) -> Path:
    root = tmp_path / "index"
    root.mkdir()
    # Every index checkout carries its own registry-host policy — the file
    # `cli/_wiring.py` loads before `validate`/`reconcile` touch a registry.
    # Seeded with the public index's own `{"ghcr.io"}` so these flows exercise
    # the shipped policy end to end.
    write_policy(root, ["ghcr.io"])
    return root
