from __future__ import annotations

from indexbot.core.catalog_md import cas_relpath, render_wrapper_page
from indexbot.model import Desc, PackageRoot


def _root(desc: Desc | None) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=desc,
    )


def test_cas_relpath_strips_sha256_prefix_from_digest() -> None:
    path = cas_relpath("kitware", "cmake", "sha256:abcd1234", "json")
    assert path == "p/kitware/cmake/o/sha256/abcd1234.json"


def test_render_wrapper_page_no_desc_degrades_gracefully() -> None:
    page = render_wrapper_page(_root(None))
    assert 'title: "ocx.sh/kitware/cmake"' in page
    assert 'description: ""' in page
    assert "keywords: []" in page
    assert "[Full README]" not in page


def test_render_wrapper_page_with_desc_and_readme_links_cas_url() -> None:
    desc = Desc(
        digest="sha256:dddd",
        title="CMake",
        description="Cross-platform build system generator.",
        keywords=("build", "cmake"),
        readme="sha256:" + "r" * 64,
        logo="sha256:" + "l" * 64,
    )
    page = render_wrapper_page(_root(desc))
    assert 'title: "CMake"' in page
    assert 'keywords: ["build", "cmake"]' in page
    assert f"[Full README](/p/kitware/cmake/o/sha256/{'r' * 64}.md)" in page


def test_render_wrapper_page_desc_without_readme_omits_link() -> None:
    desc = Desc(
        digest="sha256:dddd",
        title="CMake",
        description="Build tool.",
        logo="sha256:" + "l" * 64,
    )
    page = render_wrapper_page(_root(desc))
    assert "[Full README]" not in page
