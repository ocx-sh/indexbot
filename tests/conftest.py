"""Makes `tests/` importable as a namespace package (`tests.fakes`, etc.)
regardless of pytest's per-file collection order or which subdirectory is
invoked in isolation.

Root cause: `tests/` has no `__init__.py` (bare namespace-package
directory), so pytest's default "prepend" import mode inserts each test
subdirectory's *own* directory into `sys.path` rather than `bot/` itself —
`tests.fakes` was never importable from a sibling directory (e.g.
`tests/core/`) without this, and worse, only accidentally importable as a
bare `fakes` module when `tests/fakes/test_fakes.py` happened to collect
first in the same session. Adding `pythonpath = ["."]` to
`[tool.pytest.ini_options]` would fix this identically, but this stage may
not edit `pyproject.toml` (hard invariant) — this `conftest.py` is the
no-pyproject-edit equivalent, loaded by pytest before any test collection
regardless of which test path is invoked.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BOT_ROOT = str(Path(__file__).resolve().parent.parent)
if _BOT_ROOT not in sys.path:
    sys.path.insert(0, _BOT_ROOT)
