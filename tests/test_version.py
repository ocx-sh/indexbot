# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""`__version__` resolves from installed distribution metadata.

The lookup name drifting away from `[project] name` is exactly the failure
`importlib.metadata` reports as "not installed" rather than as an error, so it
needs a test rather than a type check.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError

import pytest

import ocx_indexbot


def test_version_is_non_empty_semver_prefix() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", ocx_indexbot.__version__), ocx_indexbot.__version__


def test_version_exported_in_all() -> None:
    assert "__version__" in ocx_indexbot.__all__


def test_resolve_version_falls_back_when_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(ocx_indexbot, "_pkg_version", _raise)
    resolve = ocx_indexbot._resolve_version  # pyright: ignore[reportPrivateUsage]
    assert resolve() == "0.0.0+unknown"
