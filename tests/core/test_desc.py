from __future__ import annotations

import dataclasses
import hashlib

import pytest

from indexbot.core.desc import DescUpdate, check_desc_change
from indexbot.errors import ValidationError
from indexbot.model import Desc
from tests.fakes import FakeRegistry

_REPO = "oci://ghcr.io/ocx-contrib/cmake"
_NAME = "ocx.sh/kitware/cmake"
_DESC_TAG = "__ocx.desc"
_TITLE_KEY = "org.opencontainers.image.title"
_DESCRIPTION_KEY = "org.opencontainers.image.description"
_KEYWORDS_KEY = "sh.ocx.keywords"


def _cas_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_no_change_both_absent_returns_none() -> None:
    registry = FakeRegistry()
    assert check_desc_change(_REPO, None, registry, name=_NAME) is None


def test_no_change_matching_digest_returns_none() -> None:
    registry = FakeRegistry(desc_digests={_REPO: "sha256:" + "a" * 64})
    current = Desc(digest="sha256:" + "a" * 64, title="CMake", description="Build tool")
    assert check_desc_change(_REPO, current, registry, name=_NAME) is None


def test_desc_disappearance_raises() -> None:
    registry = FakeRegistry(desc_digests={})
    current = Desc(digest="sha256:" + "a" * 64, title="CMake", description="Build tool")
    with pytest.raises(ValueError, match="disappeared"):
        check_desc_change(_REPO, current, registry, name=_NAME)


def test_first_publish_builds_desc_and_readme() -> None:
    readme_bytes = b"# CMake\n"
    registry = FakeRegistry(
        desc_digests={_REPO: "sha256:" + "b" * 64},
        manifests={
            (_REPO, _DESC_TAG): {
                "artifactType": "application/vnd.sh.ocx.description.v1",
                "annotations": {
                    _TITLE_KEY: "CMake",
                    _DESCRIPTION_KEY: "Cross-platform build system generator.",
                    _KEYWORDS_KEY: "build, cmake, cpp",
                },
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:" + "c" * 64},
                ],
            }
        },
        blobs={(_REPO, "sha256:" + "c" * 64): readme_bytes},
    )
    result = check_desc_change(_REPO, None, registry, name=_NAME)
    assert result is not None
    assert result.desc.digest == "sha256:" + "b" * 64
    assert result.desc.title == "CMake"
    assert result.desc.description == "Cross-platform build system generator."
    assert result.desc.keywords == ("build", "cmake", "cpp")
    assert result.desc.readme == _cas_digest(readme_bytes)
    assert result.desc.logo is None
    assert result.readme_bytes == readme_bytes
    assert result.logo_bytes is None


def test_publish_with_logo_layer() -> None:
    readme_bytes = b"# CMake\n"
    logo_bytes = b"<svg></svg>"
    registry = FakeRegistry(
        desc_digests={_REPO: "sha256:" + "d" * 64},
        manifests={
            (_REPO, _DESC_TAG): {
                "annotations": {_TITLE_KEY: "CMake", _DESCRIPTION_KEY: "desc"},
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:" + "e" * 64},
                    {"mediaType": "image/svg+xml", "digest": "sha256:" + "f" * 64},
                ],
            }
        },
        blobs={
            (_REPO, "sha256:" + "e" * 64): readme_bytes,
            (_REPO, "sha256:" + "f" * 64): logo_bytes,
        },
    )
    result = check_desc_change(_REPO, None, registry, name=_NAME)
    assert result is not None
    assert result.desc.logo == _cas_digest(logo_bytes)
    assert result.logo_bytes == logo_bytes


def test_missing_keywords_annotation_defaults_empty() -> None:
    readme_bytes = b"# CMake\n"
    registry = FakeRegistry(
        desc_digests={_REPO: "sha256:" + "1" * 64},
        manifests={
            (_REPO, _DESC_TAG): {
                "annotations": {_TITLE_KEY: "CMake", _DESCRIPTION_KEY: "desc"},
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:" + "2" * 64},
                ],
            }
        },
        blobs={(_REPO, "sha256:" + "2" * 64): readme_bytes},
    )
    result = check_desc_change(_REPO, None, registry, name=_NAME)
    assert result is not None
    assert result.desc.keywords == ()


def test_unrecognized_layer_media_type_is_ignored() -> None:
    readme_bytes = b"# CMake\n"
    registry = FakeRegistry(
        desc_digests={_REPO: "sha256:" + "4" * 64},
        manifests={
            (_REPO, _DESC_TAG): {
                "annotations": {_TITLE_KEY: "CMake", _DESCRIPTION_KEY: "desc"},
                "layers": [
                    {"mediaType": "application/vnd.unknown+json", "digest": "sha256:" + "5" * 64},
                    {"mediaType": "application/markdown", "digest": "sha256:" + "6" * 64},
                ],
            }
        },
        blobs={(_REPO, "sha256:" + "6" * 64): readme_bytes},
    )
    result = check_desc_change(_REPO, None, registry, name=_NAME)
    assert result is not None
    assert result.readme_bytes == readme_bytes
    assert result.logo_bytes is None


def test_malformed_readme_layer_digest_raises_validation_error() -> None:
    # A `__ocx.desc` manifest layer digest is registry-fetched content the
    # entry's own repository owner fully controls — a path-traversal-shaped
    # value must be rejected by `parse_digest` before it ever reaches
    # `registry.get_blob`.
    registry = FakeRegistry(
        desc_digests={_REPO: "sha256:" + "7" * 64},
        manifests={
            (_REPO, _DESC_TAG): {
                "annotations": {},
                "layers": [
                    {"mediaType": "application/markdown", "digest": "sha256:aaaa/../../evil"},
                ],
            }
        },
    )
    with pytest.raises(ValidationError, match="not a valid sha256 digest"):
        check_desc_change(_REPO, None, registry, name=_NAME)


def test_missing_readme_layer_raises() -> None:
    registry = FakeRegistry(
        desc_digests={_REPO: "sha256:" + "3" * 64},
        manifests={(_REPO, _DESC_TAG): {"annotations": {}, "layers": []}},
    )
    with pytest.raises(ValueError, match="markdown"):
        check_desc_change(_REPO, None, registry, name=_NAME)


def test_desc_update_is_frozen() -> None:
    update = DescUpdate(
        desc=Desc(digest="sha256:" + "a" * 64, title="t", description="d"),
        readme_bytes=b"x",
        logo_bytes=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        update.readme_bytes = b"y"  # type: ignore[misc]


def _readme_only_registry(digest_fill: str, annotations: dict[str, str]) -> FakeRegistry:
    """A registry serving one `__ocx.desc` with a readme layer and nothing else."""
    readme_digest = "sha256:" + digest_fill * 64
    return FakeRegistry(
        desc_digests={_REPO: "sha256:" + digest_fill * 64},
        manifests={
            (_REPO, _DESC_TAG): {
                "annotations": annotations,
                "layers": [{"mediaType": "application/markdown", "digest": readme_digest}],
            }
        },
        blobs={(_REPO, readme_digest): b"# readme\n"},
    )


def test_absent_title_annotation_falls_back_to_the_last_name_segment() -> None:
    """`desc.title` is `minLength: 1` in the schema but the annotation is optional.

    Emitting `""` produces a root that passes every PR check and then fails
    `schema:validate:rendered` after merge, blocking the deploy for every
    package. The fallback matches `ocx`'s `announce::pipeline::title`.
    """
    # The name's last segment differs from the repository's, so this pins the
    # name rung specifically — with `_NAME` both rungs answer "cmake" and the
    # assertion could not tell which one produced it.
    registry = _readme_only_registry("4", {_DESCRIPTION_KEY: "no title annotation here"})
    result = check_desc_change(_REPO, None, registry, name="ocx.sh/kitware/cmake3")
    assert result is not None
    assert result.desc.title == "cmake3"


def test_absent_title_and_nameless_entry_falls_back_to_the_repository_segment() -> None:
    """With no annotation and no usable name, the physical repository's last
    segment is the final rung — the chain never yields the empty string."""
    registry = _readme_only_registry("5", {})
    result = check_desc_change(_REPO, None, registry, name="")
    assert result is not None
    assert result.desc.title == "cmake"


def test_present_title_annotation_wins_over_every_fallback() -> None:
    registry = _readme_only_registry("6", {_TITLE_KEY: "CMake"})
    result = check_desc_change(_REPO, None, registry, name=_NAME)
    assert result is not None
    assert result.desc.title == "CMake"
