"""Integration-harness fixtures: the two socket fakes and a blank index root.

`fake_ghcr` / `fake_forge` yield running, context-managed servers (torn down
after the test). `index_tree` is a fresh, empty checkout root for
`build_git_tree` to seed — orthogonal to the servers so a test composes them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from indexbot.core.policy import INDEX_POLICY_PATH
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


@pytest.fixture
def index_tree(tmp_path: Path) -> Path:
    root = tmp_path / "index"
    root.mkdir()
    # Every index checkout carries its own registry-host policy — the file
    # `cli/_wiring.py` loads before `validate`/`reconcile` touch a registry.
    # Seeded with the public index's own `{"ghcr.io"}` so these flows exercise
    # the shipped policy end to end.
    policy = root / INDEX_POLICY_PATH
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_bytes(b'{"registry_hosts": ["ghcr.io"]}\n')
    return root
