# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""`indexbot` — the write path for an OCX package index.

The bot owns governance and integrity for a sparse HTTP index: it validates
announced package roots against registry truth, regenerates derived fields,
renders the wire tree, and enforces the governance contracts a fork-PR
announce lane depends on. It is a CLI, not a library — the module surface is
package-private and may move between releases.

Wire-format authority lives in [ocx](https://github.com/ocx-sh/ocx); this
package implements the write side of it.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

__all__ = ["__version__"]


def _resolve_version() -> str:
    """Installed distribution version, or a sentinel when the distribution is
    absent (a source checkout that was never installed).

    `PackageNotFoundError` is an `importlib` implementation detail; letting it
    escape would make a packaging exception part of this package's surface
    (PY-PKG-04).
    """
    try:
        return _pkg_version("ocx-indexbot")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _resolve_version()
