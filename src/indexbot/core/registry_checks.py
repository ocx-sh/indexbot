"""Network-touching semantic checks that reuse `RegistryPort` (CONTRACTS.md §5).

Kept separate from `core/validate_entry.py` (which is pure — no `Protocol`
argument reaches for the network at all) to make the pure/network split
visually obvious at the module level, even though both stay "pure" in
CONTRACTS.md §0's sense: deterministic given an injected `RegistryPort`,
never a direct `httpx` call.

`check_repository_allowlisted` (`core/validate_entry.py`) must always run
before either function below — SSRF ordering, ADR-4 BD-1. Neither function
here decides *disposition*: `check_digest_in_scope` raises for the one
outcome that is unambiguously wrong (a claimed digest that doesn't exist);
`check_ownership` never raises at all — the caller (`cli/validate.py`)
inspects the returned `OwnershipProbeResult` and decides block vs. WARN vs.
pass, per G-15's carry-forward table.
"""

from __future__ import annotations

from indexbot.errors import ValidationError
from indexbot.model import OwnershipProbeResult
from indexbot.ports import RegistryPort


def check_digest_in_scope(repository: str, digest: str, registry: RegistryPort) -> None:
    """G-15 digest-scope check: `digest` must exist as a manifest in THE
    ENTRY'S OWN `repository` — never any other repository.

    `registry.get_manifest` raising `KeyError` (404) means the claimed
    content digest does not actually exist on the physical repo -> re-raised
    as `ValidationError`: a claim about registry content that isn't true is a
    validation failure, not an anomaly, because nothing was ever legitimately
    observed to mutate.
    """
    try:
        registry.get_manifest(repository, digest)
    except KeyError as exc:
        raise ValidationError(
            f"digest {digest!r} does not exist on repository {repository!r} (G-15 scope check)"
        ) from exc


def check_ownership(
    repository: str, expected_name: str, registry: RegistryPort
) -> OwnershipProbeResult:
    """G-15 ownership probe — thin pass-through to `registry.probe_ownership`.

    The identifier-embedding convention this depends on is unconfirmed
    against `ocx-mirror`'s actual publishing behavior (ADR-4 Risk 2), so
    `RegistryPort.probe_ownership` is itself the pluggable seam: a
    `"mismatch"`/`"confirmed"` result is decisive, and an `"unconfirmed"`
    result (the adapter default when the embedding convention isn't found at
    all) is a structured, loud signal — never silently collapsed into either
    decisive outcome by this function.

    This function never raises and never interprets the result — `"mismatch"`
    -> block (`ValidationError`) vs. `"unconfirmed"` -> WARN-and-surface is
    the caller's (`cli/validate.py`'s) disposition to make, per CONTRACTS.md
    §5's explicit split of "probe" from "decide".
    """
    return registry.probe_ownership(repository, expected_name)
