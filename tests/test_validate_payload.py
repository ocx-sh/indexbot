from __future__ import annotations

import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from indexbot.core.validate_payload import (
    PACKAGE_ID_MAX_LENGTH,
    PACKAGE_ID_RE,
    parse_package_id,
)
from indexbot.errors import ValidationError
from indexbot.model import PackageId

# A crafted input must validate well under this bound regardless of shape or
# length — the length cap (BD-4) makes regex work non-catastrophic even for
# an adversarial, far-over-cap input, and the two-segment shape has no
# nested quantifiers so even an at-cap adversarial input is cheap.
_WALL_CLOCK_BOUND_SECONDS = 0.1


def test_parse_package_id_happy_path() -> None:
    assert parse_package_id("ocx-contrib/cmake") == PackageId(
        namespace="ocx-contrib", package="cmake"
    )


def test_parse_package_id_accepts_dotted_underscored_package_segment() -> None:
    # Exercises every separator the package shape allows: ".", "_", "__", "-+".
    assert parse_package_id("acme/lib.core_v2__beta-tools") == PackageId(
        namespace="acme", package="lib.core_v2__beta-tools"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "cmake",  # missing namespace segment
        "/cmake",  # empty namespace
        "ocx-contrib/",  # empty package
        "ocx-contrib/cmake/extra",  # too many segments
        "OCX-Contrib/cmake",  # uppercase not allowed
        "ocx_contrib/cmake",  # namespace forbids "_"
        "-ocx/cmake",  # namespace can't start with "-"
        "ocx-/cmake",  # namespace can't end with "-"
        "ocx--contrib/cmake",  # namespace forbids consecutive "-"
        "ocx.contrib/cmake",  # namespace forbids "."
        "ocx-contrib/.cmake",  # package can't start with a separator
        "ocx-contrib/cmake.",  # package can't end with a separator
    ],
)
def test_parse_package_id_rejects_shape_violations(raw: str) -> None:
    with pytest.raises(ValidationError, match="does not match the expected shape"):
        parse_package_id(raw)


def test_parse_package_id_rejects_over_combined_length_before_regex() -> None:
    # 70-char namespace + "/" + 70-char package = 141 chars, one over the
    # combined 140-char budget (ADR-2 ND-3) — must fail on length, not shape.
    raw = ("a" * 70) + "/" + ("b" * 70)
    assert len(raw) == PACKAGE_ID_MAX_LENGTH + 1
    with pytest.raises(ValidationError, match="exceeds max length 140"):
        parse_package_id(raw)


def test_parse_package_id_rejects_namespace_over_its_own_cap() -> None:
    # 40-char namespace (over the 39-char per-segment cap) + short package;
    # combined length (46) is well under 140, so only the per-segment check
    # can catch this (CONTRACTS.md §4 step 3).
    raw = ("a" * 40) + "/pkg"
    with pytest.raises(ValidationError, match="exceeds max length 39"):
        parse_package_id(raw)


def test_parse_package_id_rejects_package_over_its_own_cap_within_combined_budget() -> None:
    # 1-char namespace + 138-char package = 140 chars total, satisfying the
    # combined budget while still violating the 100-char package cap — the
    # exact scenario CONTRACTS.md §4 step 3 calls out.
    raw = "a/" + ("b" * 138)
    assert len(raw) == PACKAGE_ID_MAX_LENGTH
    with pytest.raises(ValidationError, match="exceeds max length 100"):
        parse_package_id(raw)


# --- hypothesis: acceptance property ---------------------------------------


def _within_all_caps(raw: str) -> bool:
    if len(raw) > PACKAGE_ID_MAX_LENGTH:
        return False
    namespace, package = raw.split("/", 1)
    return len(namespace) <= 39 and len(package) <= 100


@given(st.from_regex(PACKAGE_ID_RE, fullmatch=True).filter(_within_all_caps))
@settings(max_examples=200)
def test_parse_package_id_accepts_every_in_budget_regex_match(raw: str) -> None:
    namespace, package = raw.split("/", 1)
    assert parse_package_id(raw) == PackageId(namespace=namespace, package=package)


# --- hypothesis: traversal / injection rejection property -------------------

_TRAVERSAL_AND_INJECTION_PAYLOADS = (
    "..",
    "../../etc/passwd",
    "/etc/passwd",
    "ocx/..",
    "ocx/../../etc/passwd",
    "ocx/pkg/../../../etc/passwd",
    "ocx/pkg; rm -rf /",
    "ocx/pkg`whoami`",
    "ocx/pkg$(whoami)",
    "ocx/%s%s%s%s",
    "ocx/{0.__class__.__mro__}",
    "ocx/pkg\x00trailing-null",
    "ocx//pkg",
    "\\\\server\\share",
    "C:\\Windows\\System32",
)


@given(st.sampled_from(_TRAVERSAL_AND_INJECTION_PAYLOADS))
def test_parse_package_id_rejects_traversal_and_injection_payloads(raw: str) -> None:
    with pytest.raises(ValidationError):
        parse_package_id(raw)


# --- ReDoS wall-clock cap -----------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        # At the exact 140-char combined cap, deliberately shaped as a long
        # run of package-shape separators with no trailing match — the
        # closest thing to a worst case for the (non-nested) package
        # quantifier, still processed by the actual regex (length check
        # alone can't short-circuit this one).
        "a/" + "a" + "-" * 137,
        # Same idea for the namespace shape, at its own boundary.
        "a" + "-a" * 40 + "/pkg",
        # Far beyond the length cap — must be rejected by the length check
        # alone, before the regex engine ever sees it.
        ("a" * 500_000) + "/" + ("b" * 500_000),
        # A pathological, non-matching separator run far beyond the cap.
        "-" * 1_000_000,
    ],
)
def test_parse_package_id_rejects_adversarial_input_within_wall_clock_bound(raw: str) -> None:
    start = time.monotonic()
    with pytest.raises(ValidationError):
        parse_package_id(raw)
    elapsed = time.monotonic() - start
    assert elapsed < _WALL_CLOCK_BOUND_SECONDS
