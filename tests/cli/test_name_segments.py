"""An index is not required to be two segments deep.

Every other suite here describes the public index, which declares
`name_segments: 2`, so a bot that had quietly kept the old constant would
still pass all of them. These tests describe the other shapes: a flat
one-segment index and a three-segment corporate one, driven through the same
`render`/`validate` entry points a deployment actually runs.

The wire paths are the point. A three-segment index writes
`p/platform/tools/mycli.json` and its CAS objects under
`p/platform/tools/mycli/o/sha256/`, and the listing that discovers roots must
tell the two apart by depth — a root sits at exactly the declared depth, and
anything deeper is that package's own CAS subtree.
"""

from __future__ import annotations

import argparse
import json

import pytest

from ocx_indexbot.cli import render as cli_render
from ocx_indexbot.cli import validate as cli_validate
from ocx_indexbot.core.validate_entry import serialize_package_root
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import Owner, PackageRoot
from tests.fakes import InMemoryFiles, make_policy

_OWNER = Owner(github="alice", github_id=1)


def _root(name: str) -> PackageRoot:
    return PackageRoot(
        name=name,
        repository="oci://ghcr.io/acme/mycli",
        owners=(_OWNER,),
        status="active",
        deprecated_message=None,
        created="2026-08-24",
        desc=None,
        tags={},
    )


def _render(files: InMemoryFiles, **policy_overrides: object) -> ExitCode:
    args = argparse.Namespace(index_dir="", out="dist", check=False)
    return cli_render.run(args, files=files, policy=make_policy(**policy_overrides))


# --- three segments ---------------------------------------------------------


def test_three_segment_index_renders_its_own_path_shape() -> None:
    files = InMemoryFiles(
        files={
            "p/platform/tools/mycli.json": serialize_package_root(
                _root("acme.corp/platform/tools/mycli")
            )
        }
    )

    assert _render(files, name="acme.corp", name_segments=3) is ExitCode.OK

    assert "dist/p/platform/tools/mycli.json" in files.files
    config = json.loads(files.files["dist/config.json"])
    assert config["name_segments"] == 3, "config.json republishes the operator's declaration"
    index = json.loads(files.files["dist/c/index.json"])
    assert set(index["packages"]) == {"platform/tools/mycli"}


def test_three_segment_root_listing_does_not_swallow_a_cas_object() -> None:
    """`p/a/b/c.json` is a root at depth 3; `p/a/b/o/sha256/<hex>.json` is
    four deep and belongs to `p/a/b.json`. A depth-3 index must not mistake a
    depth-2 package's CAS object for a root of its own."""
    files = InMemoryFiles(
        files={
            "p/platform/tools/mycli.json": serialize_package_root(
                _root("acme.corp/platform/tools/mycli")
            ),
            f"p/platform/tools/o/sha256/{'a' * 64}.json": b"{}",
        }
    )

    assert _render(files, name="acme.corp", name_segments=3) is ExitCode.OK

    index = json.loads(files.files["dist/c/index.json"])
    assert set(index["packages"]) == {"platform/tools/mycli"}


# --- one segment ------------------------------------------------------------


def test_flat_index_renders_a_single_segment_package() -> None:
    files = InMemoryFiles(files={"p/mycli.json": serialize_package_root(_root("acme.corp/mycli"))})

    assert _render(files, name="acme.corp", name_segments=1) is ExitCode.OK

    assert "dist/p/mycli.json" in files.files
    assert json.loads(files.files["dist/config.json"])["name_segments"] == 1


# --- validate enforces the declared depth -----------------------------------


def test_validate_rejects_a_root_at_the_wrong_depth(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The depth is the index's declaration, so a two-segment path on a
    three-segment index is a policy violation, not a shape typo.

    `validate` reports per-path rather than raising — it is the PR gate and
    must show every failing root, not just the first."""
    files = InMemoryFiles(
        files={"p/tools/mycli.json": serialize_package_root(_root("acme.corp/tools/mycli"))}
    )
    args = argparse.Namespace(
        paths=["p/tools/mycli.json"],
        offline=True,
        base_dir=None,
        allow_reserved_namespace=False,
    )
    result = cli_validate.run(
        args,
        files=files,
        registry=None,  # pyright: ignore[reportArgumentType] — offline, never dialled
        policy=make_policy(name="acme.corp", name_segments=3),
    )

    assert result is ExitCode.VALIDATION_FAILURE
    assert "name_segments = 3" in capsys.readouterr().err


def test_validate_accepts_a_root_at_the_declared_depth() -> None:
    path = "p/platform/tools/mycli.json"
    files = InMemoryFiles(
        files={path: serialize_package_root(_root("acme.corp/platform/tools/mycli"))}
    )
    args = argparse.Namespace(
        paths=[path], offline=True, base_dir=None, allow_reserved_namespace=False
    )
    assert (
        cli_validate.run(
            args,
            files=files,
            registry=None,  # pyright: ignore[reportArgumentType] — offline, never dialled
            policy=make_policy(name="acme.corp", name_segments=3),
        )
        is ExitCode.OK
    )
