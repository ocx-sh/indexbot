from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ocx_indexbot.adapters.local_files import LocalFiles
from ocx_indexbot.cli.render import run
from ocx_indexbot.core.validate_entry import serialize_package_root
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import Owner, PackageRoot, TagEntry
from ocx_indexbot.ports import FilePort
from tests.fakes import InMemoryFiles, make_policy

_DIGEST = f"sha256:{'a' * 64}"


def _args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "index_dir": "",
        "out": "dist",
        "check": False,
        "allow_empty": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _root() -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(Owner(login="alice", id=1),),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags={"1.0.0": TagEntry(content=_DIGEST, observed="2026-07-17T00:00:00Z")},
    )


def _index_bytes() -> bytes:
    """One tag's CAS object: the registry's OCI image index verbatim."""
    return (
        b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json",'
        b'"manifests":[{"mediaType":"application/vnd.oci.image.manifest.v1+json",'
        b'"digest":"sha256:' + b"9" * 64 + b'","size":512,'
        b'"platform":{"architecture":"amd64","os":"linux"}}]}'
    )


def _seed_source(files: FilePort, *, index_dir: str = "") -> None:
    """Writes a one-package, no-desc source tree (`p/kitware/cmake.json` plus
    its one CAS image index) under `index_dir` -- reuses
    `validate_entry.serialize_package_root` for `root_raw` rather than
    hand-rolling a second encoder (CONTRACTS.md §1)."""
    prefix = f"{index_dir.rstrip('/')}/p/" if index_dir else "p/"
    hex_digest = _DIGEST.removeprefix("sha256:")
    files.write_bytes(f"{prefix}kitware/cmake.json", serialize_package_root(_root()))
    files.write_bytes(f"{prefix}kitware/cmake/o/sha256/{hex_digest}.json", _index_bytes())


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


def test_writes_dist_files() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    before = set(files.files)
    hex_digest = _DIGEST.removeprefix("sha256:")

    result = run(_args(out="dist"), files=files, policy=make_policy())

    assert result == ExitCode.OK
    assert set(files.files) - before == {
        "dist/config.json",
        "dist/c/index.json",
        "dist/p/kitware/cmake.json",
        f"dist/p/kitware/cmake/o/sha256/{hex_digest}.json",
    }
    assert files.read_bytes("dist/p/kitware/cmake.json") == files.read_bytes("p/kitware/cmake.json")


def test_tolerates_trailing_slash_on_out() -> None:
    files = InMemoryFiles()
    _seed_source(files)

    result = run(_args(out="dist/"), files=files, policy=make_policy())

    assert result == ExitCode.OK
    assert files.exists("dist/config.json")


def test_check_mode_clean_when_tree_already_matches_and_writes_nothing() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    run(_args(), files=files, policy=make_policy())
    snapshot = dict(files.files)

    result = run(_args(check=True), files=files, policy=make_policy())

    assert result == ExitCode.OK
    assert files.files == snapshot


def test_check_mode_drifted_when_out_tree_missing() -> None:
    files = InMemoryFiles()
    _seed_source(files)  # source present, dist never written

    result = run(_args(check=True), files=files, policy=make_policy())

    assert result == ExitCode.VALIDATION_FAILURE


def test_check_mode_drifted_when_dist_content_mismatched() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    run(_args(), files=files, policy=make_policy())
    files.write_text("dist/config.json", "{}")  # stale content vs. the current plan

    result = run(_args(check=True), files=files, policy=make_policy())

    assert result == ExitCode.VALIDATION_FAILURE


def test_check_mode_drifted_when_orphan_file_present() -> None:
    files = InMemoryFiles()
    _seed_source(files)
    run(_args(), files=files, policy=make_policy())
    # A CAS blob the current plan no longer produces (e.g. left over from a
    # previous render before its tag was repointed) -- extra, not missing.
    files.write_text("dist/p/kitware/cmake/o/sha256/" + "b" * 64 + ".json", "{}")

    result = run(_args(check=True), files=files, policy=make_policy())

    assert result == ExitCode.VALIDATION_FAILURE


def test_index_dir_prefix_is_respected() -> None:
    files = InMemoryFiles()
    _seed_source(files, index_dir="public/")  # trailing slash tolerated

    result = run(_args(index_dir="public", out="dist"), files=files, policy=make_policy())

    assert result == ExitCode.OK
    assert files.exists("dist/p/kitware/cmake.json")


def test_cas_subtree_file_without_a_root_is_ignored() -> None:
    files = InMemoryFiles(files={"p/kitware/cmake/o/sha256/" + "a" * 64 + ".json": _index_bytes()})

    result = run(_args(allow_empty=True), files=files, policy=make_policy())

    assert result == ExitCode.OK
    index = json.loads(files.read_bytes("dist/c/index.json") or b"{}")
    assert index["packages"] == {}


def test_root_vanishing_between_list_and_read_raises() -> None:
    files = _VanishingFiles(vanished_path="p/kitware/cmake.json")
    _seed_source(files)

    with pytest.raises(ValidationError, match="expected file vanished during render"):
        run(_args(), files=files, policy=make_policy())


def test_golden_plan_execution_against_real_filesystem(tmp_path: Path) -> None:
    files = LocalFiles(root=tmp_path)
    _seed_source(files)

    result = run(_args(index_dir="", out="site/.vitepress/dist"), files=files, policy=make_policy())

    assert result == ExitCode.OK

    config = json.loads((tmp_path / "site/.vitepress/dist/config.json").read_text(encoding="utf-8"))
    assert config == {"format_version": 1, "name_segments": 2}

    root_copy = (tmp_path / "site/.vitepress/dist/p/kitware/cmake.json").read_bytes()
    assert root_copy == serialize_package_root(_root())

    hex_digest = _DIGEST.removeprefix("sha256:")
    cas_copy = (
        tmp_path / f"site/.vitepress/dist/p/kitware/cmake/o/sha256/{hex_digest}.json"
    ).read_bytes()
    assert cas_copy == _index_bytes()

    index = json.loads((tmp_path / "site/.vitepress/dist/c/index.json").read_text(encoding="utf-8"))
    assert list(index["packages"]) == ["kitware/cmake"]


# ---- --index-dir: the shapes a hand-written pipeline actually types ----------


@pytest.mark.parametrize("index_dir", ["", ".", "./", "."])
def test_the_current_directory_spellings_all_find_the_roots(index_dir: str) -> None:
    """`--index-dir .` is what a hand-written pipeline types, and a `FilePort`
    key never carries that prefix — so the un-normalized `./p/` matched
    nothing and rendered a complete, valid, EMPTY index over a populated one.
    Exit 0, no warning. Measured live before this test existed."""
    files = InMemoryFiles()
    _seed_source(files)

    assert run(_args(index_dir=index_dir), files=files, policy=make_policy()) == ExitCode.OK

    index = json.loads(files.read_bytes("dist/c/index.json") or b"{}")
    assert index["packages"], "the seeded root must be in the rendered index"


def test_a_render_that_discovers_no_roots_is_refused() -> None:
    """The silent-empty path is the dangerous half of the same bug: whatever
    the reason a prefix matches nothing, the result is a deployable index that
    unpublishes every package."""
    files = InMemoryFiles()
    _seed_source(files)

    with pytest.raises(ValidationError, match="no package roots"):
        run(_args(index_dir="typo"), files=files, policy=make_policy())


def test_allow_empty_is_how_a_brand_new_index_renders() -> None:
    """An index before its first announce legitimately has no roots. That is
    the only case, and it has to be stated rather than inferred."""
    files = InMemoryFiles()

    assert run(_args(allow_empty=True), files=files, policy=make_policy()) == ExitCode.OK
