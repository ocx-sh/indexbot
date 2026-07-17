from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from indexbot.adapters.local_files import LocalFiles
from indexbot.cli.render import run
from indexbot.core.validate_entry import serialize_package_root
from indexbot.errors import ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import Owner, PackageRoot, TagEntry
from indexbot.ports import FilePort
from tests.fakes import InMemoryFiles

_DIGEST = f"sha256:{'a' * 64}"


def _args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "index_dir": "",
        "site_dist": "site",
        "out": "dist",
        "check": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _root() -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(Owner(github="alice", github_id=1),),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags={"1.0.0": TagEntry(content=_DIGEST, observed="2026-07-17T00:00:00Z")},
    )


def _observation_bytes() -> bytes:
    return (
        b'{"platforms":[{"platform":{"architecture":"amd64","os":"linux"},'
        b'"digest":"sha256:manifest-1.0.0"}]}'
    )


def _seed_source(files: FilePort, *, index_dir: str = "") -> None:
    """Writes a one-package, no-desc source tree (`p/kitware/cmake.json` plus
    its one CAS observation object) under `index_dir` -- reuses
    `validate_entry.serialize_package_root` for `root_raw` rather than
    hand-rolling a second encoder (CONTRACTS.md §1)."""
    prefix = f"{index_dir.rstrip('/')}/p/" if index_dir else "p/"
    hex_digest = _DIGEST.removeprefix("sha256:")
    files.write_bytes(f"{prefix}kitware/cmake.json", serialize_package_root(_root()))
    files.write_bytes(f"{prefix}kitware/cmake/o/sha256/{hex_digest}.json", _observation_bytes())


@dataclass
class _VanishingFiles(InMemoryFiles):
    """`FilePort` test double: `read_bytes` reports `None` for one specific
    path even though `list_files` still lists it -- simulates a file
    vanishing between being listed and read (a torn local checkout), which
    plain `InMemoryFiles` cannot model (its `files` dict is the single
    source of truth for both operations)."""

    vanished_path: str = ""

    def read_bytes(self, path: str) -> bytes | None:
        if path == self.vanished_path:
            return None
        return super().read_bytes(path)


def test_requires_at_least_one_output_tree() -> None:
    files = InMemoryFiles()
    with pytest.raises(ValidationError, match="at least one of --site-dist or --out"):
        run(_args(site_dist=None, out=None), files=files)


def test_writes_only_wrapper_pages_when_out_omitted() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    before = set(files.files)

    result = run(_args(site_dist="site", out=None), files=files)

    assert result == ExitCode.OK
    assert set(files.files) - before == {"site/kitware/cmake.md"}


def test_writes_only_dist_files_when_site_dist_omitted() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    before = set(files.files)
    hex_digest = _DIGEST.removeprefix("sha256:")

    result = run(_args(site_dist=None, out="dist"), files=files)

    assert result == ExitCode.OK
    assert set(files.files) - before == {
        "dist/config.json",
        "dist/c/index.json",
        "dist/p/kitware/cmake.json",
        f"dist/p/kitware/cmake/o/sha256/{hex_digest}.json",
        "dist/data/catalog/packages.json",
    }
    assert files.read_bytes("dist/p/kitware/cmake.json") == files.read_bytes("p/kitware/cmake.json")


def test_writes_both_trees_when_both_given_and_tolerates_trailing_slash() -> None:
    files = InMemoryFiles()
    _seed_source(files)

    result = run(_args(site_dist="site/", out="dist/"), files=files)

    assert result == ExitCode.OK
    assert files.exists("site/kitware/cmake.md")
    assert files.exists("dist/config.json")


def test_check_mode_clean_when_trees_already_match_and_writes_nothing() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    run(_args(), files=files)
    snapshot = dict(files.files)

    result = run(_args(check=True), files=files)

    assert result == ExitCode.OK
    assert files.files == snapshot


def test_check_mode_drifted_when_site_tree_missing() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    run(_args(site_dist=None, out="dist"), files=files)  # only dist written, site left absent

    result = run(_args(check=True), files=files)

    assert result == ExitCode.VALIDATION_FAILURE


def test_check_mode_drifted_when_dist_content_mismatched() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    run(_args(), files=files)
    files.write_text("dist/config.json", "{}")  # stale content vs. the current plan

    result = run(_args(check=True), files=files)

    assert result == ExitCode.VALIDATION_FAILURE


def test_check_mode_drifted_when_orphan_file_present() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    run(_args(), files=files)
    # A CAS blob the current plan no longer produces (e.g. left over from a
    # previous render before its tag was repointed) -- extra, not missing.
    files.write_text("dist/p/kitware/cmake/o/sha256/" + "b" * 64 + ".json", "{}")

    result = run(_args(check=True), files=files)

    assert result == ExitCode.VALIDATION_FAILURE


def test_index_dir_prefix_is_respected() -> None:
    files = InMemoryFiles()
    _seed_source(files, index_dir="public/")  # trailing slash tolerated

    result = run(_args(index_dir="public", site_dist="site", out=None), files=files)

    assert result == ExitCode.OK
    assert files.exists("site/kitware/cmake.md")


def test_cas_subtree_file_without_a_root_is_ignored() -> None:
    files = InMemoryFiles(
        files={"p/kitware/cmake/o/sha256/" + "a" * 64 + ".json": _observation_bytes()}
    )

    result = run(_args(), files=files)

    assert result == ExitCode.OK
    catalog = json.loads(files.read_bytes("dist/data/catalog/packages.json") or b"[]")
    assert catalog == []


def test_root_vanishing_between_list_and_read_raises() -> None:
    files = _VanishingFiles(vanished_path="p/kitware/cmake.json")
    _seed_source(files)

    with pytest.raises(ValidationError, match="expected file vanished during render"):
        run(_args(), files=files)


def test_golden_plan_execution_against_real_filesystem(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    _seed_source(files)

    result = run(_args(index_dir="", site_dist="site/src", out="site/.vitepress/dist"), files=files)

    assert result == ExitCode.OK

    wrapper = (tmp_path / "site/src/kitware/cmake.md").read_text(encoding="utf-8")
    assert "# ocx.sh/kitware/cmake" in wrapper

    config = json.loads((tmp_path / "site/.vitepress/dist/config.json").read_text(encoding="utf-8"))
    assert config == {"format_version": 1}

    root_copy = (tmp_path / "site/.vitepress/dist/p/kitware/cmake.json").read_bytes()
    assert root_copy == serialize_package_root(_root())

    hex_digest = _DIGEST.removeprefix("sha256:")
    cas_copy = (
        tmp_path / f"site/.vitepress/dist/p/kitware/cmake/o/sha256/{hex_digest}.json"
    ).read_bytes()
    assert cas_copy == _observation_bytes()

    catalog = json.loads(
        (tmp_path / "site/.vitepress/dist/data/catalog/packages.json").read_text(encoding="utf-8")
    )
    assert catalog[0]["name"] == "ocx.sh/kitware/cmake"
