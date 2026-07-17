"""`core/registry_checks.py` — the network-touching half of the G-15/digest-
scope semantic checks, exercised against `tests/fakes.FakeRegistry` (never
edited — insufficiencies would go in `open_questions`; none found)."""

from __future__ import annotations

import pytest
from fakes import FakeRegistry

from indexbot.core.registry_checks import check_digest_in_scope, check_ownership
from indexbot.errors import ValidationError


def test_check_digest_in_scope_ok_when_manifest_exists_on_own_repository() -> None:
    digest = "sha256:" + "a" * 64
    registry = FakeRegistry(manifests={("ghcr.io/ocx-contrib/cmake", digest): {"schemaVersion": 2}})
    check_digest_in_scope("ghcr.io/ocx-contrib/cmake", digest, registry)  # no raise


def test_check_digest_in_scope_missing_manifest_raises_validation_error() -> None:
    digest = "sha256:" + "a" * 64
    registry = FakeRegistry()
    with pytest.raises(ValidationError, match="G-15"):
        check_digest_in_scope("ghcr.io/ocx-contrib/cmake", digest, registry)


def test_check_digest_in_scope_only_checks_the_entrys_own_repository() -> None:
    # A digest that exists on a *different* repository must not satisfy the
    # scope check — the whole point of "own repository" in G-15.
    digest = "sha256:" + "a" * 64
    registry = FakeRegistry(
        manifests={("ghcr.io/ocx-contrib/other-package", digest): {"schemaVersion": 2}}
    )
    with pytest.raises(ValidationError):
        check_digest_in_scope("ghcr.io/ocx-contrib/cmake", digest, registry)


def test_check_ownership_confirmed_passes_through() -> None:
    registry = FakeRegistry(ownership={"ghcr.io/ocx-contrib/cmake": "confirmed"})
    result = check_ownership("ghcr.io/ocx-contrib/cmake", "ocx.sh/kitware/cmake", registry)
    assert result == "confirmed"


def test_check_ownership_mismatch_passes_through_without_raising() -> None:
    # Disposition (block vs. WARN) is the caller's job — this function must
    # never itself raise on "mismatch", only surface it.
    registry = FakeRegistry(ownership={"ghcr.io/ocx-contrib/cmake": "mismatch"})
    result = check_ownership("ghcr.io/ocx-contrib/cmake", "ocx.sh/kitware/cmake", registry)
    assert result == "mismatch"


def test_check_ownership_unconfirmed_is_the_loud_default_never_a_silent_pass() -> None:
    # FakeRegistry's own default (no ownership configured) is "unconfirmed"
    # — the structured, surfaced-not-silent outcome ADR-4 Risk 2 requires
    # when the identifier-embedding convention isn't found at all.
    registry = FakeRegistry()
    result = check_ownership("ghcr.io/ocx-contrib/cmake", "ocx.sh/kitware/cmake", registry)
    assert result == "unconfirmed"
