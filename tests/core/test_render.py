"""Golden fixture tests for `core/render.py` (WP2-F).

Each case builds `SourcePackage` fixtures as plain Python literals (no need
to round-trip through JSON just to build a fixture — CONTRACTS.md §2), runs
`build_render_plan`, and byte-compares every produced `FileWrite` against a
committed expected file under `tests/golden/render/<case>/expected/dist/**`
— and asserts no *extra* files were produced, so orphan pruning is verified
by absence, not just by what's present.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from indexbot.core.render import FileWrite, SourcePackage, build_render_plan
from indexbot.model import Desc, Owner, PackageId, PackageRoot, TagEntry, Yank

_GOLDEN_ROOT = Path(__file__).parent.parent / "golden" / "render"


def _digest(letter: str) -> str:
    return f"sha256:{letter * 64}"


def _owner() -> Owner:
    return Owner(github="alice", github_id=1)


_DEFAULT_PLATFORM = {"architecture": "amd64", "os": "linux"}

_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"


def _descriptor(tag_hint: str, platform: dict[str, str] | None) -> dict[str, object]:
    """One `manifests[]` entry. `platform=None` emits a platform-less
    descriptor — legal OCI (the field is optional) and one of the shapes
    `_catalog_platforms` must skip; the others are `platform.os == "unknown"`
    and a `platform` object missing `os` or `architecture` (ADR R4), which is
    why the label reads both fields with `.get`. `digest` is a real sha256
    over a label rather than a placeholder string, so the fixture reads like
    the registry bytes it stands in for."""
    label = (
        f"{tag_hint}/attached"
        if platform is None
        else f"{tag_hint}/{platform.get('os', '?')}/{platform.get('architecture', '?')}"
    )
    descriptor: dict[str, object] = {
        "mediaType": _MANIFEST_MEDIA_TYPE,
        "digest": f"sha256:{hashlib.sha256(label.encode()).hexdigest()}",
        "size": 512,
    }
    if platform is not None:
        descriptor["platform"] = platform
    return descriptor


def _index_of(*descriptors: dict[str, object]) -> bytes:
    """One tag's CAS bytes: the registry's OCI image index, as `render.py`
    receives it. `render.py` copies these verbatim into the dist tree, so
    every golden CAS object under `expected/dist/p/**/o/sha256/*.json` is
    exactly this output."""
    return json.dumps(
        {"schemaVersion": 2, "mediaType": _INDEX_MEDIA_TYPE, "manifests": list(descriptors)}
    ).encode()


def _index_bytes(
    tag_hint: str, *, platforms: tuple[dict[str, str], ...] = (_DEFAULT_PLATFORM,)
) -> bytes:
    """The common case: one runnable-image descriptor per platform."""
    return _index_of(*(_descriptor(tag_hint, platform) for platform in platforms))


def _root_raw(root: PackageRoot) -> bytes:
    """Test-local, minimal root -> JSON serialization — intentionally
    decoupled from `core.validate_entry.serialize_package_root` (a
    different work package) so these fixtures don't depend on its exact
    output shape. `render.py` only ever copies `root_raw` verbatim; these
    bytes just need to look like a real root for fixture readability."""
    payload: dict[str, object] = {
        "name": root.name,
        "repository": root.repository,
        "owners": [{"github": o.github, "github_id": o.github_id} for o in root.owners],
        "status": root.status,
        "deprecated_message": root.deprecated_message,
        "created": root.created,
    }
    if root.desc is None:
        payload["desc"] = None
    else:
        desc_dict: dict[str, object] = {
            "digest": root.desc.digest,
            "title": root.desc.title,
            "description": root.desc.description,
            "keywords": list(root.desc.keywords),
        }
        if root.desc.readme is not None:
            desc_dict["readme"] = root.desc.readme
        if root.desc.logo is not None:
            desc_dict["logo"] = root.desc.logo
        payload["desc"] = desc_dict
    if root.source is not None:
        payload["source"] = root.source
    tags_dict: dict[str, object] = {}
    for tag, entry in root.tags.items():
        tag_dict: dict[str, object] = {"content": entry.content, "observed": entry.observed}
        if entry.yanked is not None:
            tag_dict["yanked"] = {"reason": entry.yanked.reason, "at": entry.yanked.at}
        tags_dict[tag] = tag_dict
    payload["tags"] = tags_dict
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _package(
    *,
    namespace: str,
    package: str,
    repository: str,
    created: str,
    tags: dict[str, TagEntry],
    desc: Desc | None,
    content_by_digest: dict[str, bytes],
    source: str | None = None,
) -> SourcePackage:
    root = PackageRoot(
        name=f"ocx.sh/{namespace}/{package}",
        repository=repository,
        owners=(_owner(),),
        status="active",
        deprecated_message=None,
        created=created,
        desc=desc,
        source=source,
        tags=tags,
    )
    return SourcePackage(
        package_id=PackageId(namespace=namespace, package=package),
        root=root,
        root_raw=_root_raw(root),
        content_by_digest=content_by_digest,
    )


def _case_normal() -> list[SourcePackage]:
    tags = {
        "1.0.0": TagEntry(content=_digest("1"), observed="2026-07-17T00:00:00Z"),
        "0.9.0": TagEntry(content=_digest("2"), observed="2026-07-16T00:00:00Z"),
    }
    desc = Desc(
        digest=_digest("d"),
        title="CMake",
        description="Cross-platform build system generator.",
        keywords=("build", "cmake"),
        readme=_digest("r"),
        logo=_digest("l"),
    )
    content_by_digest = {
        # Different platform sets across the two live tags -- exercises
        # `_catalog_platforms`' union + dedup (linux/amd64 shared by both) +
        # sort (darwin/arm64 sorts before linux/amd64).
        f"{_digest('1')}.json": _index_bytes(
            "1.0.0", platforms=({"architecture": "amd64", "os": "linux"},)
        ),
        f"{_digest('2')}.json": _index_bytes(
            "0.9.0",
            platforms=(
                {"architecture": "amd64", "os": "linux"},
                {"architecture": "arm64", "os": "darwin"},
            ),
        ),
        f"{_digest('r')}.md": b"# CMake\n\nCross-platform build system generator.\n",
        f"{_digest('l')}.svg": b"<svg>cmake-logo</svg>",
    }
    return [
        _package(
            namespace="kitware",
            package="cmake",
            repository="oci://ghcr.io/ocx-contrib/cmake",
            created="2026-07-17",
            tags=tags,
            desc=desc,
            content_by_digest=content_by_digest,
            # The one case carrying `source` — proves the field rides through
            # the /p/** wire mirror verbatim (and into c/index.json's root
            # digest) without leaking into the catalog view-model, which has
            # no repository column to put it in.
            source="https://github.com/ocx-contrib/mirror-cmake",
        )
    ]


def _case_orphan_pruned() -> list[SourcePackage]:
    tags = {"1.2.0": TagEntry(content=_digest("a"), observed="2026-07-17T00:00:00Z")}
    content_by_digest = {
        f"{_digest('a')}.json": _index_bytes("1.2.0"),
        # referenced by no tag and no desc -> pruned from dist, never emitted.
        f"{_digest('b')}.json": _index_bytes("orphaned"),
    }
    return [
        _package(
            namespace="oven-sh",
            package="bun",
            repository="oci://ghcr.io/ocx-contrib/bun",
            created="2026-07-10",
            tags=tags,
            desc=None,
            content_by_digest=content_by_digest,
        )
    ]


def _case_yanked_excluded() -> list[SourcePackage]:
    # `yanked.at` (2026-07-20) is strictly later than every live tag's
    # `observed` (2026-07-17) -- proves `_generated_timestamp` actually folds
    # in `yanked.at`, not just `observed` (finding #4: deleting that branch
    # used to fail nothing here, since 07-17 already dominated 07-02). The
    # yanked tag's image index also carries a platform
    # (windows/amd64) distinct from the live tag's (linux/amd64), so the
    # golden `platforms` array proves exclusion by absence, not coincidence
    # (finding #5: the two used to share a platform, making exclusion
    # unfalsifiable).
    tags = {
        "1.0.0": TagEntry(content=_digest("x"), observed="2026-07-17T00:00:00Z"),
        "0.9.0": TagEntry(
            content=_digest("y"),
            observed="2026-07-01T00:00:00Z",
            yanked=Yank(reason="broken build", at="2026-07-20T00:00:00Z"),
        ),
    }
    content_by_digest = {
        f"{_digest('x')}.json": _index_bytes("1.0.0"),
        # only referenced by the yanked tag, no live tag shares it -> pruned.
        f"{_digest('y')}.json": _index_bytes(
            "0.9.0-yanked", platforms=({"architecture": "amd64", "os": "windows"},)
        ),
    }
    return [
        _package(
            namespace="astral-sh",
            package="uv",
            repository="oci://ghcr.io/ocx-contrib/uv",
            created="2026-06-01",
            tags=tags,
            desc=None,
            content_by_digest=content_by_digest,
        )
    ]


def _case_shared_digest_dedup() -> list[SourcePackage]:
    shared = _digest("z")
    tags = {
        "0.13.0": TagEntry(content=shared, observed="2026-07-17T00:00:00Z"),
        "latest": TagEntry(content=shared, observed="2026-07-17T00:00:00Z"),
    }
    content_by_digest = {f"{shared}.json": _index_bytes("0.13.0")}
    return [
        _package(
            namespace="ziglang",
            package="zig",
            repository="oci://ghcr.io/ocx-contrib/zig",
            created="2026-05-01",
            tags=tags,
            desc=None,
            content_by_digest=content_by_digest,
        )
    ]


def _case_no_desc() -> list[SourcePackage]:
    tags = {"3.7.0": TagEntry(content=_digest("s"), observed="2026-07-17T00:00:00Z")}
    content_by_digest = {f"{_digest('s')}.json": _index_bytes("3.7.0")}
    return [
        _package(
            namespace="mvdan",
            package="shfmt",
            repository="oci://ghcr.io/ocx-contrib/shfmt",
            created="2026-04-01",
            tags=tags,
            desc=None,
            content_by_digest=content_by_digest,
        )
    ]


def _case_png_only_logo() -> list[SourcePackage]:
    tags = {"1.42.0": TagEntry(content=_digest("g"), observed="2026-07-17T00:00:00Z")}
    desc = Desc(
        digest=_digest("e"),
        title="glab",
        description="GitLab CLI.",
        keywords=("git", "cli"),
        readme=None,
        logo=_digest("p"),
    )
    content_by_digest = {
        f"{_digest('g')}.json": _index_bytes("1.42.0"),
        f"{_digest('p')}.png": b"\x89PNG\r\n\x1a\nfake-glab-logo",
    }
    return [
        _package(
            namespace="gitlab-org",
            package="glab",
            repository="oci://ghcr.io/ocx-contrib/glab",
            created="2026-03-01",
            tags=tags,
            desc=desc,
            content_by_digest=content_by_digest,
        )
    ]


def _case_nested_namespace() -> list[SourcePackage]:
    tags = {"0.7.0": TagEntry(content=_digest("n"), observed="2026-07-17T00:00:00Z")}
    desc = Desc(
        digest=_digest("m"),
        title="regsync",
        description="Utility to sync images between registries.",
        keywords=("oci", "registry", "sync"),
        readme=_digest("q"),
        logo=_digest("w"),
    )
    content_by_digest = {
        f"{_digest('n')}.json": _index_bytes("0.7.0"),
        f"{_digest('q')}.md": b"# regsync\n\nUtility to sync images between registries.\n",
        f"{_digest('w')}.svg": b"<svg>regsync-logo</svg>",
    }
    return [
        _package(
            namespace="regclient",
            package="regsync",
            repository="oci://ghcr.io/ocx-contrib/regclient-regsync",
            created="2026-02-01",
            tags=tags,
            desc=desc,
            content_by_digest=content_by_digest,
        )
    ]


def _case_attestation_descriptor() -> list[SourcePackage]:
    """ADR R4. A real registry serves attestation and referrer descriptors
    inside the same image index as the runnable ones — cosign/buildkit
    attestations carry `platform: {"os": "unknown", ...}`, and `platform` is
    an optional descriptor field so a referrer may carry none at all. The
    fourth shape is a `platform` object that names only one of the two
    fields: nothing upstream of render validates `platform` at all (the
    image-index gate stops at each descriptor's `digest`), so these are raw
    publisher bytes and a partial object must be skipped, not crash the
    whole-index render. None of the three names a target anything can run
    on, so none may reach the catalog's `platforms` matrix; the golden's
    `catalog.json` shows them absent. Authored from the ADR before any
    golden was regenerated.
    """
    tags = {"2.0.0": TagEntry(content=_digest("t"), observed="2026-07-17T00:00:00Z")}
    content_by_digest = {
        f"{_digest('t')}.json": _index_of(
            _descriptor("2.0.0", {"architecture": "amd64", "os": "linux"}),
            _descriptor("2.0.0", {"architecture": "arm64", "os": "linux"}),
            _descriptor("2.0.0", {"architecture": "unknown", "os": "unknown"}),
            _descriptor("2.0.0", None),
            _descriptor("2.0.0", {"os": "linux"}),
        )
    }
    return [
        _package(
            namespace="sigstore",
            package="cosign",
            repository="oci://ghcr.io/ocx-contrib/cosign",
            created="2026-01-01",
            tags=tags,
            desc=None,
            content_by_digest=content_by_digest,
        )
    ]


def _assert_tree_matches(root: Path, files: tuple[FileWrite, ...]) -> None:
    produced = {fw.path: fw.content for fw in files}
    expected_paths = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert set(produced) == expected_paths
    for path, content in produced.items():
        actual = content.encode("utf-8") if isinstance(content, str) else content
        expected = (root / path).read_bytes()
        assert actual == expected


def _assert_matches_golden(case: str, plan: tuple[FileWrite, ...]) -> None:
    case_dir = _GOLDEN_ROOT / case / "expected"
    _assert_tree_matches(case_dir / "dist", plan)


def test_render_normal() -> None:
    _assert_matches_golden("normal", build_render_plan(_case_normal()))


def test_render_orphan_pruned() -> None:
    _assert_matches_golden("orphan_pruned", build_render_plan(_case_orphan_pruned()))


def test_render_yanked_excluded() -> None:
    _assert_matches_golden("yanked_excluded", build_render_plan(_case_yanked_excluded()))


def test_render_shared_digest_dedup() -> None:
    _assert_matches_golden("shared_digest_dedup", build_render_plan(_case_shared_digest_dedup()))


def test_render_no_desc() -> None:
    _assert_matches_golden("no_desc", build_render_plan(_case_no_desc()))


def test_render_png_only_logo() -> None:
    _assert_matches_golden("png_only_logo", build_render_plan(_case_png_only_logo()))


def test_render_nested_namespace() -> None:
    _assert_matches_golden("nested_namespace", build_render_plan(_case_nested_namespace()))


def test_render_attestation_descriptor_skips_platformless_and_unknown() -> None:
    _assert_matches_golden(
        "attestation_descriptor", build_render_plan(_case_attestation_descriptor())
    )


def test_catalog_platforms_skips_platformless_unknown_and_partial_descriptors() -> None:
    # The golden above pins the whole tree; this pins the one claim the
    # scenario exists for, readably and independently of the byte compare.
    plan = build_render_plan(_case_attestation_descriptor())
    catalog_file = next(fw for fw in plan if fw.path == "data/catalog/catalog.json")
    assert isinstance(catalog_file.content, str)
    catalog = json.loads(catalog_file.content)
    assert catalog["packages"][0]["platforms"] == ["linux/amd64", "linux/arm64"]


def test_build_render_plan_sorts_packages_by_package_id() -> None:
    # Passed out of alphabetical order; "kitware/cmake" < "mvdan/shfmt".
    packages = _case_no_desc() + _case_normal()
    plan = build_render_plan(packages)
    catalog_file = next(fw for fw in plan if fw.path == "data/catalog/catalog.json")
    assert isinstance(catalog_file.content, str)
    catalog = json.loads(catalog_file.content)
    assert [p["name"] for p in catalog["packages"]] == [
        "ocx.sh/kitware/cmake",
        "ocx.sh/mvdan/shfmt",
    ]


def test_build_render_plan_respects_format_version_param() -> None:
    plan = build_render_plan(_case_no_desc(), format_version=7)
    config = next(fw for fw in plan if fw.path == "config.json")
    assert isinstance(config.content, str)
    assert json.loads(config.content) == {"format_version": 7, "name_segments": 2}


def test_build_render_plan_package_index_digest_matches_root_raw_sha256() -> None:
    packages = _case_no_desc()
    plan = build_render_plan(packages)
    index_file = next(fw for fw in plan if fw.path == "c/index.json")
    assert isinstance(index_file.content, str)
    index = json.loads(index_file.content)
    assert index["format_version"] == 1
    expected_digest = f"sha256:{hashlib.sha256(packages[0].root_raw).hexdigest()}"
    assert index["packages"] == {"mvdan/shfmt": expected_digest}


def test_build_render_plan_package_index_empty_for_no_packages() -> None:
    plan = build_render_plan([])
    index_file = next(fw for fw in plan if fw.path == "c/index.json")
    assert isinstance(index_file.content, str)
    assert json.loads(index_file.content) == {"format_version": 1, "packages": {}}


def test_catalog_updated_null_for_tagless_package() -> None:
    # `_latest_activity`'s `default=None` branch: a root that has never
    # carried a tag yields `updated: null` (and a null catalog `generated`).
    packages = [
        _package(
            namespace="kitware",
            package="cmake",
            repository="oci://ghcr.io/ocx-contrib/cmake",
            created="2026-01-01",
            tags={},
            desc=None,
            content_by_digest={},
        )
    ]
    plan = build_render_plan(packages)
    catalog_file = next(fw for fw in plan if fw.path == "data/catalog/catalog.json")
    assert isinstance(catalog_file.content, str)
    catalog = json.loads(catalog_file.content)
    assert catalog["generated"] is None
    assert catalog["packages"][0]["created"] == "2026-01-01"
    assert catalog["packages"][0]["updated"] is None


def test_build_render_plan_catalog_generated_null_for_no_packages() -> None:
    plan = build_render_plan([])
    catalog_file = next(fw for fw in plan if fw.path == "data/catalog/catalog.json")
    assert isinstance(catalog_file.content, str)
    assert json.loads(catalog_file.content) == {"generated": None, "packages": []}


def test_build_render_plan_reachability_readme_without_logo() -> None:
    # `png_only_logo` covers desc.readme is None / desc.logo is not None; this
    # covers the complementary combo (readme set, logo unset) so both
    # branches of `_reachable_digests`' independent `if`s are exercised.
    tags = {"1.0.0": TagEntry(content=_digest("k"), observed="2026-07-17T00:00:00Z")}
    desc = Desc(
        digest=_digest("j"),
        title="shfmt",
        description="Shell formatter.",
        readme=_digest("h"),
        logo=None,
    )
    content_by_digest = {
        f"{_digest('k')}.json": _index_bytes("1.0.0"),
        f"{_digest('h')}.md": b"# shfmt\n",
    }
    package = _package(
        namespace="mvdan",
        package="shfmt2",
        repository="oci://ghcr.io/ocx-contrib/shfmt2",
        created="2026-04-01",
        tags=tags,
        desc=desc,
        content_by_digest=content_by_digest,
    )
    plan = build_render_plan([package])
    dist_paths = {fw.path for fw in plan}
    assert f"p/mvdan/shfmt2/o/sha256/{'h' * 64}.md" in dist_paths
    catalog_file = next(fw for fw in plan if fw.path == "data/catalog/catalog.json")
    catalog = json.loads(catalog_file.content)
    assert catalog["packages"][0]["logoUrl"] is None
    assert catalog["packages"][0]["readmeUrl"] == f"/p/mvdan/shfmt2/o/sha256/{'h' * 64}.md"
