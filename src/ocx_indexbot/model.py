"""Data model mirroring ADR-1's package-root field table.

Plain, frozen, slotted data only — no validation logic. Format validation
against `schema/*.json` runs via `check-jsonschema` (never imported here);
semantic checks (path<->name derivation, digest `fullmatch`, host allowlist,
...) are `core/validate_entry.py` (Phase 2; `core/validate_payload.py`
merged into it, fork-PR announce revamp). Every type here is immutable by
construction: the bot never mutates an observed value in place, it always
computes a new one and rebinds.
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
"""`ForgePort.set_commit_status`'s state — the `governance/review-required`
gate (ADR-4 BD-5).

GitHub's Commit Status vocabulary, kept verbatim because it is the wider of
the two: GitLab has no `error`, so `adapters/gitlab_api.py` folds both
`failure` and `error` onto its `failed`. Narrowing this alias to the
intersection would instead throw away a distinction GitHub can express."""


@dataclass(frozen=True, slots=True)
class ManifestFetch:
    """Return of `RegistryPort.get_manifest` — a CAS-verifiable manifest read.

    ADR-1's verifiability chain requires every digest this bot records to be
    *derivable from content*, never synthesized and never trusted from a
    response header alone. `digest` is therefore always computed by the
    implementing adapter as `sha256:<hex of raw>` — never copied verbatim
    from a registry-supplied header (see `ports.py`'s docstring for the full
    doctrine and `adapters/registry_v2.py`'s verify-if-present header check).

    `raw` is the exact wire bytes the registry served (the CAS-verifiable
    input `digest` was computed over); `parsed` is that same content decoded
    as JSON, for callers that only need structured field access.
    """

    raw: bytes
    digest: str
    parsed: dict[str, object]


@dataclass(frozen=True, slots=True)
class Owner:
    """One entry in `owners[]` — and, reused verbatim, one entry in
    `.github/maintainers.yml` (ADR-1 D2, renamed forge-neutral in
    `adr_forge_neutral_owners.md` D1).

    `login` is a **username**: the handle a forge API resolves to a numeric
    user id. It is never a display name — `ForgePort.request_reviewers` hands
    this exact string to the forge, and on GitLab that is
    `GET /users?username=<login>`, whose `name` field is a different thing
    entirely (the human's full name). Requesting review by display name
    resolves to nobody, silently.

    `id` is mandatory and is the ownership key: it survives a forge username
    rename or recycling, which `login` alone does not. G-19 matches the pull
    request author against it and never against `login`
    (`cli/governance_check.py`).

    Neither field names a forge, deliberately. The same two fields carry a
    GitHub login and user id on a GitHub-hosted index and a GitLab username
    and user id on a GitLab-hosted one; an index reachable from
    `.github/index-policy.json`'s `ci.forge` knows which. The wire keeps the
    pre-0.5.0 spelling (`github`, `github_id`) alongside these, derived, for
    one release — see `core/validate_entry.py`'s codec.
    """

    login: str
    id: int


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

    `content` is the digest of the OCI image index this tag resolved to, as
    served by the physical registry; those exact bytes are stored at
    `p/<ns>/<pkg>/o/sha256/<hex>.json`. One digest namespace: the registry
    computed it over the same bytes this index commits.
    """

    content: str
    observed: str
    yanked: Yank | None = None


@dataclass(frozen=True, slots=True)
class PackageId:
    """The logical id parsed from a `p/**.json` path or `cli/announce.py`'s
    `--package` argument.

    Holds `name_segments` already-validated segments — two under the default
    (`kitware/cmake`), but an index declares its own depth, so this type
    deliberately exposes no `namespace`/`package` pair. A one-segment index
    has no such split to expose, and every caller that wanted one was really
    building the joined path that `str(package_id)` already returns.

    Distinct from `PackageRoot.name`, which is the full
    `<index name>/<segments>` form. Shape validated by
    `core/validate_entry.py`'s `parse_package_id`.
    """

    segments: tuple[str, ...]

    def __str__(self) -> str:
        return "/".join(self.segments)


@dataclass(frozen=True, slots=True)
class PullRequestInfo:
    """Base/head SHAs and changed paths for one PR (`ForgePort.get_pull_request_info`).

    `cli/classify_pr.py`'s only input — read via the GitHub API diff, never
    a checkout (ADR-4 BD-5's `governance-gate` trust boundary: `changed_paths`
    is what G-04's "added `p/*.json` path" and G-05's human-review key-set
    checks key off).

    `author_login`/`author_id` (fork-PR announce revamp, G-19): the PR
    author's GitHub identity — `author_id` is the stable numeric id (survives
    username rename/recycling, same rationale as `Owner.id`),
    `author_login` the current login (self-review carve-out display /
    reviewer-list filtering, `cli/governance_check.py`). `cli/classify_pr.py`
    does not read either field; they exist for `governance_check.py`'s G-19
    "PR author id in every touched package's `owners[]`" gate.
    Defaulted (not required) so every existing `classify_pr`-only construction
    site stays unchanged; a caller that needs G-19 always sets both.

    `updated_at`/`labels` (`indexbot stale`, WP5-C): the forge's own
    last-activity timestamp (RFC 3339, whole-day granularity is all `stale`
    compares on) and the PR's current label set. Both already ride along in
    the same API response `get_pull_request_info` was already fetching on
    both forges — no second round trip. Defaulted empty for the same reason
    `author_login`/`author_id` are: every pre-existing construction site
    (`classify_pr`, `governance_check`, every test fixture) stays unchanged;
    `stale` is the one caller that requires both to be real.
    """

    number: int
    base_sha: str
    head_sha: str
    changed_paths: tuple[str, ...]
    author_login: str = ""
    author_id: int = 0
    updated_at: str = ""
    labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PullRequestHeadMatch:
    """Result of `ForgePort.find_pull_request_by_head_sha` — `cli/label_failed_run.py`'s
    only way to turn a completed run's head commit back into a pull request.

    `number` is the PR/MR number whose CURRENT head is exactly the queried
    SHA. `is_fork` is ADR-6 FP-8's scoping rule made queryable: `checks-failed`
    labeling (and the stale-close it later feeds) applies to fork-authored
    pull requests only — a same-repository PR's failing checks are already
    visible to every maintainer with push access, so labeling it would just be
    noise the label was never meant to carry.
    """

    number: int
    is_fork: bool


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

    `source` defaults to `None` — the `org.opencontainers.image.source`
    annotation of the latest version's observed manifest
    (`core/observe.py` -> `core/regenerate.py`), i.e. the repository whose CI
    produced the builds. Bot-derived, same omit-when-absent contract as
    `superseded_by`. Distinct from `upstream.repository_url`, which is
    human-governed vendor attribution — for a mirror the two name different
    repositories on purpose.

    `variants` defaults to `()` and is **retired** — no writer records it any
    more. `regenerate` always leaves it empty and `ocx package announce`
    removes it, because two publishers disagreeing about whether to write a
    derived field makes them alternate: one restores the key, the next opens a
    pull request that only deletes it. It stays in the model so a root still
    carrying the key parses and round-trips byte-identically until its next
    announce drops it. The variant names a consumer wants are derived from
    `tags` (`core/version_order.py`'s `variant_names`), which is what
    `core/render.py` puts in the catalog. Empty serializes as an omitted key
    rather than `[]`, so every root published before this field existed stays
    byte-identical.
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
    source: str | None = None
    variants: tuple[str, ...] = ()
    tags: dict[str, TagEntry] = field(default_factory=_empty_tags)
