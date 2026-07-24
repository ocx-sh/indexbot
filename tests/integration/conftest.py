"""Integration-harness fixtures: the two socket fakes and a blank index root.

`fake_ghcr` / `fake_forge` yield running, context-managed servers (torn down
after the test). `index_tree` is a fresh, empty checkout root for
`build_git_tree` to seed — orthogonal to the servers so a test composes them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

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
    return root
