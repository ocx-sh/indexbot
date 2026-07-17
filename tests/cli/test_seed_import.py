from __future__ import annotations

import argparse
import json

import pytest

from indexbot.cli.seed_import import run
from indexbot.errors import ValidationError
from indexbot.exit_codes import ExitCode
from tests.fakes import FakeRegistry, FixedClock, InMemoryFiles

_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_BARE_MANIFEST_AMD64: dict[str, object] = {"platform": {"architecture": "amd64", "os": "linux"}}

_CATALOG_MD = """---
title: CMake
description: Cross-platform build system generator.
keywords: build, cmake, cpp
---
# CMake

Full readme content for CMake.
"""

_CATALOG_MD_NO_KEYWORDS = """---
title: CMake
description: Cross-platform build system generator.
---
Body without a keywords field.
"""

_MIRROR_YML = """# seed mirror config
repository: oci://ghcr.io/ocx-contrib/cmake
"""

_MIRROR_YML_TARGET_ALLOWLISTED = """target:
  registry: ghcr.io
  repository: ocx-contrib/cmake
"""

_MIRROR_YML_TARGET_NOT_ALLOWLISTED = """target:
  registry: ocx.sh
  repository: cmake
"""

_MIRROR_YML_TARGET_MISSING_REPOSITORY_KEY = """target:
  registry: ghcr.io
"""

_MIRROR_YML_INDENTED_LINE_WITHOUT_PARENT = """repository: oci://ghcr.io/ocx-contrib/cmake
  extra: value
"""


def _args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "catalog_md": "kitware/cmake/CATALOG.md",
        "mirror_yml": "kitware/cmake/mirror.yml",
        "logo": None,
        "namespace": "kitware",
        "package": "cmake",
        "out": "p",
        "owner_github": "alice",
        "owner_github_id": 123456,
        "upstream_org": None,
        "upstream_repository_url": None,
        "upstream_disclaimer": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _files(**extra: bytes | str) -> InMemoryFiles:
    files = InMemoryFiles(
        files={
            "kitware/cmake/CATALOG.md": _CATALOG_MD.encode("utf-8"),
            "kitware/cmake/mirror.yml": _MIRROR_YML.encode("utf-8"),
        }
    )
    for path, content in extra.items():
        files.files[path] = content.encode("utf-8") if isinstance(content, str) else content
    return files


def _registry(
    *,
    tags: dict[str, list[str]] | None = None,
    manifests: dict[tuple[str, str], dict[str, object]] | None = None,
) -> FakeRegistry:
    return FakeRegistry(
        tags=tags if tags is not None else {_REPO: ["3.28.1"]},
        manifests=manifests if manifests is not None else {(_REPO, "3.28.1"): _BARE_MANIFEST_AMD64},
    )


def test_happy_path_writes_root_and_cas_objects() -> None:
    files = _files()
    result = run(_args(), registry=_registry(), files=files, clock=FixedClock())

    assert result == ExitCode.OK
    root_bytes = files.read_bytes("p/kitware/cmake.json")
    assert root_bytes is not None
    root = json.loads(root_bytes)

    assert root["name"] == "ocx.sh/kitware/cmake"
    assert root["repository"] == _REPO
    assert root["owners"] == [{"github": "alice", "github_id": 123456}]
    assert root["status"] == "active"
    assert root["deprecated_message"] is None
    assert root["created"] == "2026-07-17"
    assert "upstream" not in root
    assert root["desc"]["title"] == "CMake"
    assert root["desc"]["description"] == "Cross-platform build system generator."
    assert root["desc"]["keywords"] == ["build", "cmake", "cpp"]
    assert root["desc"]["digest"].startswith("sha256:")
    assert len(root["desc"]["digest"]) == len("sha256:") + 64

    tag = root["tags"]["3.28.1"]
    assert tag["observed"] == "2026-07-17T00:00:00Z"
    content_digest = tag["content"]
    assert content_digest.startswith("sha256:")

    cas_hex = content_digest.removeprefix("sha256:")
    observation_bytes = files.read_bytes(f"p/kitware/cmake/o/sha256/{cas_hex}.json")
    assert observation_bytes is not None
    assert json.loads(observation_bytes)["platforms"][0]["platform"]["architecture"] == "amd64"

    readme_digest = root["desc"]["readme"]
    readme_hex = readme_digest.removeprefix("sha256:")
    readme_bytes = files.read_bytes(f"p/kitware/cmake/o/sha256/{readme_hex}.md")
    assert readme_bytes == b"# CMake\n\nFull readme content for CMake."

    assert "logo" not in root["desc"]


def test_happy_path_with_logo_writes_svg_cas_object() -> None:
    files = _files(**{"kitware/cmake/logo.svg": b"<svg></svg>"})
    result = run(
        _args(logo="kitware/cmake/logo.svg"), registry=_registry(), files=files, clock=FixedClock()
    )
    assert result == ExitCode.OK

    root = json.loads(files.read_bytes("p/kitware/cmake.json") or b"{}")
    logo_digest = root["desc"]["logo"]
    logo_hex = logo_digest.removeprefix("sha256:")
    logo_bytes = files.read_bytes(f"p/kitware/cmake/o/sha256/{logo_hex}.svg")
    assert logo_bytes == b"<svg></svg>"


def test_happy_path_with_png_logo() -> None:
    files = _files(**{"kitware/cmake/logo.png": b"\x89PNG"})
    result = run(
        _args(logo="kitware/cmake/logo.png"), registry=_registry(), files=files, clock=FixedClock()
    )
    assert result == ExitCode.OK
    root = json.loads(files.read_bytes("p/kitware/cmake.json") or b"{}")
    logo_hex = root["desc"]["logo"].removeprefix("sha256:")
    assert files.read_bytes(f"p/kitware/cmake/o/sha256/{logo_hex}.png") == b"\x89PNG"


def test_catalog_md_without_keywords_defaults_empty() -> None:
    files = _files(**{"kitware/cmake/CATALOG.md": _CATALOG_MD_NO_KEYWORDS})
    run(_args(), registry=_registry(), files=files, clock=FixedClock())
    root = json.loads(files.read_bytes("p/kitware/cmake.json") or b"{}")
    assert root["desc"]["keywords"] == []


def test_upstream_fields_populate_upstream_object() -> None:
    files = _files()
    run(
        _args(
            upstream_org="Kitware",
            upstream_repository_url="https://github.com/Kitware/CMake",
            upstream_disclaimer="Mirrored by OCX; not affiliated with Kitware.",
        ),
        registry=_registry(),
        files=files,
        clock=FixedClock(),
    )
    root = json.loads(files.read_bytes("p/kitware/cmake.json") or b"{}")
    assert root["upstream"] == {
        "org": "Kitware",
        "repository_url": "https://github.com/Kitware/CMake",
        "disclaimer": "Mirrored by OCX; not affiliated with Kitware.",
    }


def test_owner_github_id_coerced_from_string() -> None:
    files = _files()
    run(_args(owner_github_id="123456"), registry=_registry(), files=files, clock=FixedClock())
    root = json.loads(files.read_bytes("p/kitware/cmake.json") or b"{}")
    assert root["owners"][0]["github_id"] == 123456


def test_derives_namespace_and_package_from_catalog_md_path() -> None:
    files = _files()
    run(
        _args(namespace=None, package=None),
        registry=_registry(),
        files=files,
        clock=FixedClock(),
    )
    assert files.exists("p/kitware/cmake.json")


def test_derive_from_too_short_path_raises() -> None:
    files = _files()
    with pytest.raises(ValidationError, match="cannot derive namespace/package"):
        run(
            _args(namespace=None, package=None, catalog_md="CATALOG.md"),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )


def test_namespace_without_package_raises() -> None:
    files = _files()
    with pytest.raises(ValidationError, match="must be given together"):
        run(
            _args(namespace="kitware", package=None),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )


def test_shared_content_digest_dedups_to_one_cas_object() -> None:
    files = _files()
    registry = _registry(
        tags={_REPO: ["3.28.1", "latest"]},
        manifests={
            (_REPO, "3.28.1"): _BARE_MANIFEST_AMD64,
            (_REPO, "latest"): _BARE_MANIFEST_AMD64,
        },
    )
    run(_args(), registry=registry, files=files, clock=FixedClock())
    root = json.loads(files.read_bytes("p/kitware/cmake.json") or b"{}")
    digests = {tag["content"] for tag in root["tags"].values()}
    assert digests == {root["tags"]["3.28.1"]["content"]}
    cas_prefix = "p/kitware/cmake/o/sha256/"
    cas_files = [p for p in files.files if p.startswith(cas_prefix) and p.endswith(".json")]
    assert len(cas_files) == 1


def test_no_observable_tags_raises() -> None:
    files = _files()
    registry = _registry(tags={_REPO: []}, manifests={})
    with pytest.raises(ValidationError, match="no observable tags"):
        run(_args(), registry=registry, files=files, clock=FixedClock())


def test_missing_catalog_md_raises() -> None:
    files = InMemoryFiles(files={"kitware/cmake/mirror.yml": _MIRROR_YML.encode("utf-8")})
    with pytest.raises(ValidationError, match="does not exist"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_missing_mirror_yml_raises() -> None:
    files = InMemoryFiles(files={"kitware/cmake/CATALOG.md": _CATALOG_MD.encode("utf-8")})
    with pytest.raises(ValidationError, match="does not exist"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_catalog_md_missing_opening_delimiter_raises() -> None:
    files = _files(**{"kitware/cmake/CATALOG.md": "# no frontmatter here\n"})
    with pytest.raises(ValidationError, match="missing frontmatter"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_catalog_md_unclosed_frontmatter_raises() -> None:
    files = _files(**{"kitware/cmake/CATALOG.md": "---\ntitle: CMake\n"})
    with pytest.raises(ValidationError, match="never closes"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_catalog_md_malformed_frontmatter_line_raises() -> None:
    files = _files(**{"kitware/cmake/CATALOG.md": "---\nnot-a-key-value-line\n---\nbody\n"})
    with pytest.raises(ValidationError, match="malformed frontmatter line"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_catalog_md_missing_title_raises() -> None:
    files = _files(**{"kitware/cmake/CATALOG.md": "---\ndescription: x\n---\nbody\n"})
    with pytest.raises(ValidationError, match="missing required 'title'"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_catalog_md_missing_description_raises() -> None:
    files = _files(**{"kitware/cmake/CATALOG.md": "---\ntitle: CMake\n---\nbody\n"})
    with pytest.raises(ValidationError, match="missing required 'description'"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_catalog_md_blank_line_inside_frontmatter_is_skipped() -> None:
    files = _files(
        **{
            "kitware/cmake/CATALOG.md": (
                "---\ntitle: CMake\n\ndescription: Build tool\n---\nbody text\n"
            )
        }
    )
    run(_args(), registry=_registry(), files=files, clock=FixedClock())
    root = json.loads(files.read_bytes("p/kitware/cmake.json") or b"{}")
    assert root["desc"]["title"] == "CMake"


def test_mirror_yml_missing_repository_key_raises() -> None:
    files = _files(**{"kitware/cmake/mirror.yml": "unrelated_key: value\n"})
    with pytest.raises(ValidationError, match="missing required 'repository' key"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_mirror_yml_malformed_line_raises() -> None:
    files = _files(**{"kitware/cmake/mirror.yml": "not-a-key-value-line\n"})
    with pytest.raises(ValidationError, match="malformed line"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_mirror_yml_comment_and_blank_lines_are_skipped() -> None:
    files = _files(
        **{
            "kitware/cmake/mirror.yml": (
                "# a comment\n\nrepository: oci://ghcr.io/ocx-contrib/cmake\n"
            )
        }
    )
    run(_args(), registry=_registry(), files=files, clock=FixedClock())
    assert files.exists("p/kitware/cmake.json")


def test_repository_not_allowlisted_raises() -> None:
    files = _files(**{"kitware/cmake/mirror.yml": "repository: oci://docker.io/kitware/cmake\n"})
    with pytest.raises(ValidationError, match="not allowlisted"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_repository_invalid_shape_raises() -> None:
    files = _files(**{"kitware/cmake/mirror.yml": "repository: oci://ghcr.io/UPPERCASE\n"})
    with pytest.raises(ValidationError, match="OCI repository grammar"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_reserved_namespace_raises() -> None:
    files = _files()
    with pytest.raises(ValidationError, match="reserved"):
        run(
            _args(namespace="admin", package="cmake"),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )


def test_root_already_exists_raises() -> None:
    files = _files()
    files.write_text("p/kitware/cmake.json", "{}")
    with pytest.raises(ValidationError, match="already exists"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_logo_path_missing_file_raises() -> None:
    files = _files()
    with pytest.raises(ValidationError, match="does not exist"):
        run(
            _args(logo="kitware/cmake/logo.svg"),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )


def test_logo_unsupported_extension_raises() -> None:
    files = _files(**{"kitware/cmake/logo.gif": b"GIF89a"})
    with pytest.raises(ValidationError, match="unsupported logo extension"):
        run(
            _args(logo="kitware/cmake/logo.gif"),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )


def test_custom_out_dir() -> None:
    files = _files()
    run(_args(out="dist/p"), registry=_registry(), files=files, clock=FixedClock())
    assert files.exists("dist/p/kitware/cmake.json")


# --- mirror.yml nested `target:` shape (real ocx-contrib mirror shape) ----


def test_mirror_yml_nested_target_shape_allowlisted_resolves_repository() -> None:
    files = _files(**{"kitware/cmake/mirror.yml": _MIRROR_YML_TARGET_ALLOWLISTED})
    result = run(_args(), registry=_registry(), files=files, clock=FixedClock())
    assert result == ExitCode.OK
    root = json.loads(files.read_bytes("p/kitware/cmake.json") or b"{}")
    assert root["repository"] == _REPO


def test_mirror_yml_nested_target_shape_not_allowlisted_raises_precise_error() -> None:
    files = _files(**{"kitware/cmake/mirror.yml": _MIRROR_YML_TARGET_NOT_ALLOWLISTED})
    with pytest.raises(ValidationError) as excinfo:
        run(_args(), registry=_registry(), files=files, clock=FixedClock())
    message = str(excinfo.value)
    assert "ocx.sh" in message
    assert "not an allowlisted physical registry" in message
    assert "M-1" in message


def test_mirror_yml_nested_target_missing_repository_key_raises() -> None:
    files = _files(**{"kitware/cmake/mirror.yml": _MIRROR_YML_TARGET_MISSING_REPOSITORY_KEY})
    with pytest.raises(ValidationError, match="missing required 'registry'/'repository' keys"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


def test_mirror_yml_indented_line_without_parent_mapping_raises() -> None:
    files = _files(**{"kitware/cmake/mirror.yml": _MIRROR_YML_INDENTED_LINE_WITHOUT_PARENT})
    with pytest.raises(ValidationError, match="has no parent mapping"):
        run(_args(), registry=_registry(), files=files, clock=FixedClock())


# --- --repository override -------------------------------------------------


def test_repository_override_bypasses_a_non_allowlisted_mirror_yml() -> None:
    # mirror.yml alone would raise (M-1 dependency) — --repository is the
    # post-M-1 escape hatch, never touching that failing resolution path.
    files = _files(**{"kitware/cmake/mirror.yml": _MIRROR_YML_TARGET_NOT_ALLOWLISTED})
    result = run(_args(repository=_REPO), registry=_registry(), files=files, clock=FixedClock())
    assert result == ExitCode.OK
    root = json.loads(files.read_bytes("p/kitware/cmake.json") or b"{}")
    assert root["repository"] == _REPO


def test_repository_override_rejected_host_raises() -> None:
    files = _files()
    with pytest.raises(ValidationError, match="not allowlisted"):
        run(
            _args(repository="oci://docker.io/kitware/cmake"),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )


def test_repository_override_malformed_shape_raises() -> None:
    files = _files()
    with pytest.raises(ValidationError, match="OCI repository grammar"):
        run(
            _args(repository="oci://ghcr.io/UPPERCASE"),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )


# --- --allow-reserved-namespace (mechanism only; policy PR-gated) ---------


def test_ocx_namespace_blocked_by_default() -> None:
    files = _files()
    with pytest.raises(ValidationError, match="reserved"):
        run(
            _args(namespace="ocx", package="cli"),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )


def test_allow_reserved_namespace_admits_brand_segment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = _files()
    result = run(
        _args(namespace="ocx", package="cli", allow_reserved_namespace=True),
        registry=_registry(),
        files=files,
        clock=FixedClock(),
    )
    assert result == ExitCode.OK
    assert files.exists("p/ocx/cli.json")
    assert "seed-import: --allow-reserved-namespace used for ocx/cli" in capsys.readouterr().err


def test_allow_reserved_namespace_does_not_admit_control_path_segment() -> None:
    files = _files()
    with pytest.raises(ValidationError, match="reserved"):
        run(
            _args(namespace="p", package="cmake", allow_reserved_namespace=True),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )


def test_allow_reserved_namespace_does_not_admit_generic_segment() -> None:
    files = _files()
    with pytest.raises(ValidationError, match="reserved"):
        run(
            _args(namespace="admin", package="cmake", allow_reserved_namespace=True),
            registry=_registry(),
            files=files,
            clock=FixedClock(),
        )
