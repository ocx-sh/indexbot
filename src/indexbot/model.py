"""Data model mirroring ADR-1's root and observation-object field tables.

Plain, frozen, slotted data only — no validation logic. Format validation
against `schema/*.json` runs via `check-jsonschema` (never imported here);
semantic checks (path<->name derivation, digest `fullmatch`, host allowlist,
...) are `core/validate_entry.py` / `core/validate_payload.py` (Phase 2).
Every type here is immutable by construction: the bot never mutates an
observed value in place, it always computes a new one and rebinds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Status = Literal["active", "deprecated", "yanked"]
"""`PackageRoot.status` (ADR-1 D2)."""

OwnershipProbeResult = Literal["confirmed", "mismatch", "unconfirmed"]
"""`RegistryPort.probe_ownership`'s outcome (G-15, ADR-4 carry-forward table).

`"confirmed"`/`"mismatch"` are decisive; `"unconfirmed"` means the
identifier-embedding convention was not found at all — never treated as a
silent pass by `core/validate_entry.py` either way (ADR-4 Risk 2).
"""

CommitStatusState = Literal["success", "failure", "pending", "error"]
"""GitHub Commit Status API state — `GitHubPort.set_commit_status`'s
mechanism for the `governance/review-required` required status check
(ADR-4 BD-5)."""


@dataclass(frozen=True, slots=True)
class ManifestFetch:
    """Return of `RegistryPort.get_manifest` — a CAS-verifiable manifest read.

    ADR-1's verifiability chain requires every digest this bot records to be
    *derivable from content*, never synthesized and never trusted from a
    response header alone. `digest` is therefore always computed by the
    implementing adapter as `sha256:<hex of raw>` — never copied verbatim
    from a registry-supplied header (see `ports.py`'s docstring for the full
    doctrine and `adapters/ghcr.py`'s verify-if-present header check).

    `raw` is the exact wire bytes the registry served (the CAS-verifiable
    input `digest` was computed over); `parsed` is that same content decoded
    as JSON, for callers that only need structured field access.
    """

    raw: bytes
    digest: str
    parsed: dict[str, object]


@dataclass(frozen=True, slots=True)
class Owner:
    """One entry in `owners[]` (ADR-1 D2).

    `github_id` is mandatory — it survives GitHub username rename/recycling,
    unlike `github` alone.
    """

    github: str
    github_id: int


@dataclass(frozen=True, slots=True)
class Upstream:
    """`upstream` field (ADR-1 D2) — attribution of the mirrored project,
    distinct from the namespace owner."""

    org: str
    repository_url: str | None = None
    disclaimer: str | None = None


@dataclass(frozen=True, slots=True)
class Desc:
    """`desc` field (ADR-1 D6) — bot-regenerated from the physical
    registry's `__ocx.desc` tag; nullable at the root (`desc: null`) for a
    package that has never published one."""

    digest: str
    title: str
    description: str
    keywords: tuple[str, ...] = ()
    readme: str | None = None
    logo: str | None = None


@dataclass(frozen=True, slots=True)
class Yank:
    """`tags[tag].yanked` (ADR-1 D2) — presence on a tag row marks it yanked."""

    reason: str
    at: str


@dataclass(frozen=True, slots=True)
class TagEntry:
    """One row of the `tags` map (ADR-1 D2).

    `content` addresses this index's own package-local CAS (an
    `ObservationObject`), never an OCI manifest or image-index digest
    directly.
    """

    content: str
    observed: str
    yanked: Yank | None = None


@dataclass(frozen=True, slots=True)
class OciPlatform:
    """Inline subset of the OCI image-spec `Platform` object (ADR-1 D4)."""

    architecture: str
    os: str
    os_version: str | None = None
    os_features: tuple[str, ...] = ()
    variant: str | None = None
    features: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlatformEntry:
    """One row of an observation object's `platforms[]` (ADR-1 D4).

    `digest` is the OCI manifest digest on the *physical* registry — a
    different digest namespace from `TagEntry.content`, which addresses this
    index's own CAS.
    """

    platform: OciPlatform
    digest: str


@dataclass(frozen=True, slots=True)
class ObservationObject:
    """Immutable, package-local CAS object at `o/sha256/<hex>.json` (ADR-1 D4).

    Carries no timestamps, deliberately — two observations with an identical
    platform set produce byte-identical JSON, hence maximal dedup.
    """

    platforms: tuple[PlatformEntry, ...]


@dataclass(frozen=True, slots=True)
class PackageId:
    """`<namespace>/<package>` — the logical id parsed from a
    `p/<ns>/<pkg>.json` path or a `repository_dispatch` payload's `package`
    field. Distinct from `PackageRoot.name`, which is the full
    `ocx.sh/<namespace>/<package>` form. Format validated by
    `core/validate_payload.py`'s `PACKAGE_ID_RE` (Phase 2); this type only
    carries the two already-validated parts.
    """

    namespace: str
    package: str

    def __str__(self) -> str:
        return f"{self.namespace}/{self.package}"


@dataclass(frozen=True, slots=True)
class PullRequestInfo:
    """Base/head SHAs and changed paths for one PR (`GitHubPort.get_pull_request_info`).

    `cli/classify_pr.py`'s only input — read via the GitHub API diff, never
    a checkout (ADR-4 BD-5's `governance-gate` trust boundary: `changed_paths`
    is what G-04's "added `p/*.json` path" and G-05's human-review key-set
    checks key off).
    """

    number: int
    base_sha: str
    head_sha: str
    changed_paths: tuple[str, ...]


def _empty_tags() -> dict[str, TagEntry]:
    """Typed `default_factory` — a bare `dict` loses the `TagEntry` value
    type under strict type checking."""
    return {}


@dataclass(frozen=True, slots=True)
class PackageRoot:
    """`/p/<ns>/<pkg>.json` — the package root (ADR-1 D2).

    `upstream` defaults to `None` — omitted entirely for OCX's own
    first-party namespaces (ADR-2 ND-9/ND-10); `schema/root.schema.json`
    deliberately excludes it from the root's `required` list.

    `tags` defaults to an empty map for the not-yet-observed case (a
    freshly-claimed namespace before the first `announce`).

    `superseded_by` defaults to `None` — omitted entirely for a package that
    has not been superseded (schema forbids `null` there, mirroring
    `upstream`'s omit-when-absent contract). When set, it names the
    successor package's `<namespace>/<package>` id.
    """

    name: str
    repository: str
    owners: tuple[Owner, ...]
    status: Status
    deprecated_message: str | None
    created: str
    desc: Desc | None
    upstream: Upstream | None = None
    superseded_by: str | None = None
    tags: dict[str, TagEntry] = field(default_factory=_empty_tags)
