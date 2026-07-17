"""Tests for `adapters/local_files.py` — CONTRACTS.md §11.

Traversal matrix (`..`, absolute path, symlink escape), atomic-write
behavior on simulated mid-write failure, and the round-trip/`list_files`
behaviors `InMemoryFiles` (`tests/fakes`) already covers as a fake.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

import pytest

from indexbot.adapters import local_files
from indexbot.adapters.local_files import LocalFiles
from indexbot.errors import ValidationError
from indexbot.ports import FilePort


def test_write_and_read_text_round_trip(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    files.write_text("p/kitware/cmake.json", "{}")
    assert files.read_text("p/kitware/cmake.json") == "{}"


def test_write_and_read_bytes_round_trip(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    files.write_bytes("p/kitware/cmake/o/sha256/aaaa.svg", b"<svg/>")
    assert files.read_bytes("p/kitware/cmake/o/sha256/aaaa.svg") == b"<svg/>"


def test_read_text_missing_returns_none(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    assert files.read_text("missing.json") is None


def test_read_bytes_missing_returns_none(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    assert files.read_bytes("missing.svg") is None


def test_exists(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    assert files.exists("a.txt") is False
    files.write_text("a.txt", "x")
    assert files.exists("a.txt") is True


def test_write_text_creates_parent_directories(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    files.write_text("p/kitware/cmake.json", "{}")
    assert (tmp_path / "p" / "kitware" / "cmake.json").is_file()


def test_write_text_overwrites_existing_content(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    files.write_text("a.txt", "first")
    files.write_text("a.txt", "second")
    assert files.read_text("a.txt") == "second"


def test_list_files_empty_for_missing_prefix(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    assert files.list_files("p/kitware") == []


def test_list_files_returns_sorted_posix_relative_paths(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    files.write_text("p/kitware/cmake.json", "{}")
    files.write_text("p/astral-sh/uv.json", "{}")
    assert files.list_files("p") == ["p/astral-sh/uv.json", "p/kitware/cmake.json"]


def test_list_files_respects_directory_boundary(tmp_path: Path) -> None:
    # "p/kitware" must not match "p/kitware-fork/x.json" — prefix matching is
    # directory-scoped (walked via the resolved directory), not a raw string
    # prefix, matching InMemoryFiles' fake behavior exactly.
    files = LocalFiles(root=tmp_path)
    files.write_text("p/kitware/cmake.json", "{}")
    files.write_text("p/kitware-fork/x.json", "{}")
    assert files.list_files("p/kitware") == ["p/kitware/cmake.json"]


# --- traversal matrix ------------------------------------------------------


def test_dotdot_traversal_rejected_on_read(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    files = LocalFiles(root=root)
    with pytest.raises(ValidationError, match="escapes root"):
        files.read_text("../outside.txt")


def test_dotdot_traversal_rejected_before_write(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    files = LocalFiles(root=root)
    with pytest.raises(ValidationError, match="escapes root"):
        files.write_text("../escape.txt", "pwned")
    assert not (tmp_path / "escape.txt").exists()


def test_absolute_path_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    files = LocalFiles(root=root)
    with pytest.raises(ValidationError, match="escapes root"):
        files.read_text(str(outside))


def test_list_files_rejects_traversal_prefix(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    files = LocalFiles(root=root)
    with pytest.raises(ValidationError, match="escapes root"):
        files.list_files("..")


def test_symlink_escape_rejected(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(outside)

    files = LocalFiles(root=root)
    with pytest.raises(ValidationError, match="escapes root"):
        files.read_text("escape/secret.txt")


def test_symlink_escape_rejected_before_write(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path_factory.mktemp("outside")
    (root / "escape").symlink_to(outside)

    files = LocalFiles(root=root)
    with pytest.raises(ValidationError, match="escapes root"):
        files.write_text("escape/pwned.txt", "pwned")
    assert not (outside / "pwned.txt").exists()


# --- atomic writes -----------------------------------------------------


class _FailingHandle:
    """Wraps a real binary file handle, writing partial bytes then failing —
    simulates a mid-write I/O error (e.g. disk full) for the atomicity test.
    """

    def __init__(self, real: IO[bytes]) -> None:
        self._real = real

    def __enter__(self) -> _FailingHandle:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self._real.close()
        return False

    def write(self, data: bytes) -> int:
        self._real.write(data[: len(data) // 2])
        raise OSError("simulated disk failure mid-write")


def test_write_atomic_leaves_no_partial_file_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = LocalFiles(root=tmp_path)
    files.write_text("pkg.json", "original")
    real_fdopen = os.fdopen

    def failing_fdopen(fd: int, mode: str) -> _FailingHandle:
        return _FailingHandle(real_fdopen(fd, mode))

    monkeypatch.setattr(local_files.os, "fdopen", failing_fdopen)

    with pytest.raises(OSError, match="simulated disk failure"):
        files.write_text("pkg.json", "replacement content, longer than original")

    # Target file is untouched — the failed write only ever touched the tmp
    # file, never `pkg.json` itself (rename happens only after a full write).
    assert files.read_text("pkg.json") == "original"
    # The tmp file is cleaned up on failure, not left behind.
    assert list(tmp_path.glob(".pkg.json.*.tmp")) == []


def test_local_files_conforms_to_file_port(tmp_path: Path) -> None:
    files: FilePort = LocalFiles(root=tmp_path)
    assert files.list_files("") == []
