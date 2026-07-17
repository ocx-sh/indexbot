"""Path-safe local filesystem — `FilePort` implementation (CONTRACTS.md §11).

Every method resolves its `path` (or `list_files`'s `prefix`) against a fixed
`root` and rejects any resolved path that is not `.is_relative_to(root)` —
one check that catches `..`-traversal, absolute-path attempts, and symlink
escapes alike (`Path.resolve()` follows symlinks before the containment
check runs), always *before* touching the filesystem (ADR-4 BD-4).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from indexbot.errors import ValidationError


@dataclass(frozen=True, slots=True)
class LocalFiles:
    """`FilePort` over a fixed root directory (e.g. the repo checkout root)."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def read_text(self, path: str) -> str | None:
        resolved = self._resolve(path)
        if not resolved.is_file():
            return None
        return resolved.read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> None:
        self._write_atomic(self._resolve(path), content.encode("utf-8"))

    def read_bytes(self, path: str) -> bytes | None:
        resolved = self._resolve(path)
        if not resolved.is_file():
            return None
        return resolved.read_bytes()

    def write_bytes(self, path: str, content: bytes) -> None:
        self._write_atomic(self._resolve(path), content)

    def exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    def list_files(self, prefix: str) -> list[str]:
        base = self._resolve(prefix)
        if not base.is_dir():
            return []
        return sorted(
            entry.relative_to(self.root).as_posix() for entry in base.rglob("*") if entry.is_file()
        )

    def _resolve(self, path: str) -> Path:
        resolved = Path(self.root, path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValidationError(f"path escapes root: {path!r}")
        return resolved

    def _write_atomic(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
