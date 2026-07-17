from __future__ import annotations

import dataclasses

import pytest

from indexbot.model import (
    Desc,
    ManifestFetch,
    ObservationObject,
    OciPlatform,
    Owner,
    PackageId,
    PackageRoot,
    PlatformEntry,
    PullRequestInfo,
    TagEntry,
    Upstream,
    Yank,
)


def test_owner_fields() -> None:
    owner = Owner(github="alice", github_id=123456)
    assert owner.github == "alice"
    assert owner.github_id == 123456


def test_owner_is_frozen() -> None:
    owner = Owner(github="alice", github_id=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        owner.github = "bob"  # type: ignore[misc]


def test_upstream_defaults() -> None:
    upstream = Upstream(org="Kitware")
    assert upstream.repository_url is None
    assert upstream.disclaimer is None


def test_upstream_full() -> None:
    upstream = Upstream(
        org="Kitware",
        repository_url="https://github.com/Kitware/CMake",
        disclaimer="Unofficial mirror.",
    )
    assert upstream.repository_url == "https://github.com/Kitware/CMake"
    assert upstream.disclaimer == "Unofficial mirror."


def test_desc_defaults() -> None:
    desc = Desc(digest="sha256:aaa", title="CMake", description="Build tool")
    assert desc.keywords == ()
    assert desc.readme is None
    assert desc.logo is None


def test_desc_full() -> None:
    desc = Desc(
        digest="sha256:9f2c",
        title="CMake",
        description="Cross-platform build system generator.",
        keywords=("build", "cmake", "cpp"),
        readme="sha256:1a2b",
        logo="sha256:3c4d",
    )
    assert desc.keywords == ("build", "cmake", "cpp")
    assert desc.readme == "sha256:1a2b"


def test_yank_fields() -> None:
    yank = Yank(reason="broken build", at="2026-07-17T00:00:00Z")
    assert yank.reason == "broken build"
    assert yank.at == "2026-07-17T00:00:00Z"


def test_tag_entry_default_not_yanked() -> None:
    tag = TagEntry(content="sha256:aaaa", observed="2026-07-17T00:00:00Z")
    assert tag.yanked is None


def test_tag_entry_yanked() -> None:
    yank = Yank(reason="cve", at="2026-07-17T00:00:00Z")
    tag = TagEntry(content="sha256:aaaa", observed="2026-07-17T00:00:00Z", yanked=yank)
    assert tag.yanked is yank


def test_oci_platform_minimal() -> None:
    platform = OciPlatform(architecture="amd64", os="linux")
    assert platform.os_version is None
    assert platform.os_features == ()
    assert platform.variant is None
    assert platform.features == ()


def test_oci_platform_full() -> None:
    platform = OciPlatform(
        architecture="arm",
        os="linux",
        os_version="1.0",
        os_features=("sse4",),
        variant="v7",
        features=("f1",),
    )
    assert platform.variant == "v7"
    assert platform.os_features == ("sse4",)


def test_platform_entry() -> None:
    platform = OciPlatform(architecture="arm64", os="linux")
    entry = PlatformEntry(platform=platform, digest="sha256:1111")
    assert entry.platform.architecture == "arm64"
    assert entry.digest == "sha256:1111"


def test_observation_object() -> None:
    platform = OciPlatform(architecture="amd64", os="linux")
    entry = PlatformEntry(platform=platform, digest="sha256:1111")
    obj = ObservationObject(platforms=(entry,))
    assert obj.platforms == (entry,)


def test_package_id_str_and_fields() -> None:
    pkg = PackageId(namespace="kitware", package="cmake")
    assert pkg.namespace == "kitware"
    assert pkg.package == "cmake"
    assert str(pkg) == "kitware/cmake"


def test_package_root_example_from_adr_1() -> None:
    owner = Owner(github="alice", github_id=123456)
    upstream = Upstream(org="Kitware", repository_url="https://github.com/Kitware/CMake")
    desc = Desc(
        digest="sha256:9f2c",
        title="CMake",
        description="Cross-platform build system generator.",
        keywords=("build", "cmake", "cpp"),
        readme="sha256:1a2b",
        logo="sha256:3c4d",
    )
    tag = TagEntry(content="sha256:aaaa", observed="2026-07-17T00:00:00Z")
    root = PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(owner,),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        upstream=upstream,
        desc=desc,
        tags={"3.28.1": tag},
    )
    assert root.name == "ocx.sh/kitware/cmake"
    assert root.tags["3.28.1"] is tag
    assert root.status == "active"


def test_package_root_omits_upstream_for_first_party() -> None:
    root = PackageRoot(
        name="ocx.sh/ocx/cli",
        repository="oci://ghcr.io/ocx-contrib/cli",
        owners=(),
        status="active",
        deprecated_message=None,
        created="2026-06-01",
        desc=None,
    )
    assert root.upstream is None


def test_package_root_tags_default_empty() -> None:
    upstream = Upstream(org="Kitware")
    root = PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        upstream=upstream,
        desc=None,
    )
    assert root.tags == {}
    assert root.desc is None


def test_pull_request_info_fields() -> None:
    info = PullRequestInfo(
        number=42,
        base_sha="aaa111",
        head_sha="bbb222",
        changed_paths=("p/kitware/cmake.json",),
    )
    assert info.number == 42
    assert info.base_sha == "aaa111"
    assert info.head_sha == "bbb222"
    assert info.changed_paths == ("p/kitware/cmake.json",)


def test_pull_request_info_is_frozen() -> None:
    info = PullRequestInfo(number=1, base_sha="a", head_sha="b", changed_paths=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.number = 2  # type: ignore[misc]


def test_manifest_fetch_fields() -> None:
    fetch = ManifestFetch(raw=b'{"a":1}', digest="sha256:aaaa", parsed={"a": 1})
    assert fetch.raw == b'{"a":1}'
    assert fetch.digest == "sha256:aaaa"
    assert fetch.parsed == {"a": 1}


def test_manifest_fetch_is_frozen() -> None:
    fetch = ManifestFetch(raw=b"{}", digest="sha256:aaaa", parsed={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        fetch.digest = "sha256:bbbb"  # type: ignore[misc]


def test_package_root_is_frozen() -> None:
    upstream = Upstream(org="Kitware")
    root = PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(),
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        upstream=upstream,
        desc=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        root.status = "yanked"  # type: ignore[misc]
