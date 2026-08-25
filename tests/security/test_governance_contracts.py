"""One named test per governance contract G-01..G-20 (spec X7, register §5).

Contract wording: `docs/reference/contracts.md` §12, ADR-4
(`adr_index_bot_and_workflow_security.md`) Governance-Contract table +
Amendment A1, ADR-6 (`adr_fork_pr_announce.md`) FP-1..FP-9. G-08/G-17/G-18 are
RETIRED under the fork-PR lane (ADR-4 Amendment A1) — their tests are ABSENCE
tests. G-19/G-20 are the fork-PR additions. ADR-6 FP-5 (machine-lane content
is only authorized package refreshes) carries its own named test alongside
G-19 — it is a distinct contract from G-19's ownership check, not a second
test of it: FP-5 bounds *which files* may ride the machine lane, G-19 bounds
*who* may send them.

Every test names its contract id so this file reads as the audit index. Where
an existing suite already pins the exact invariant (`test_validate_entry.py`,
`test_classify_pr.py`, `test_anomaly.py`, `test_governance_check.py`,
`test_diff.py`) the named test here is a deliberate, self-contained DAMP
wrapper asserting the same invariant under its contract id — the duplication
is the deliverable, not accidental drift. ADR-2 ND-4 (reserved namespace
segments) carries its own named test alongside the G-* contracts, same
convention as FP-5 above — it is not itself a G-number, but WP-11's security
review made a currently-unreserved segment a live URL-routing collision, and
this suite is the one this repo's per-contract test bar requires that change
land beside.

PARKED BY THE EXTRACTION (2026-08-24) — assertions that read the *index
repository's* committed files, which no longer sit beside this package. They
are not deleted: they still run in `ocx-sh/index`, and the extraction plan's
WP-3 re-homes each one, either into `indexbot workflows-check` (asserted here
against fixture workflow trees) or into that repo's own `task policy:check`.
Until WP-3 lands, this file is NOT the complete audit index:

- `test_g01_schema_shape_rejects_malformed_root` — shelled out to
  `check-jsonschema` against the served `schema/root.schema.json`. Stays with
  the schema, which is OCX spec surface, not package data.
- `test_g03_shipped_policy_is_exactly_the_two_ocx_operated_hosts` and
  `test_g03_shipped_policy_is_servable_by_an_adapter` — assert *that*
  deployment's committed `.github/index-policy.json`. G-03's mechanism is
  still covered here by `test_g03_repository_host_allowlisted`.
- `test_g14_workflows_permissions_default_deny_and_sha_pinned`,
  `test_g16_privileged_unprivileged_split`,
  `test_g17_no_announce_pat_surface`, and the `announce.yml`-absence half of
  `test_g08_no_repository_dispatch_surface` — hand-parsed the real workflow
  tree. These become `indexbot workflows-check`, which infers the privileged
  job from its trigger rather than naming it, so it holds for any index repo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from ocx_indexbot.cli import governance_check, reconcile
from ocx_indexbot.cli import render as cli_render
from ocx_indexbot.core.anomaly import AnomalyFinding, check_tag_mutations
from ocx_indexbot.core.backoff import BackoffPolicy, delay_seconds, is_retryable_status
from ocx_indexbot.core.diff import classify_change, diff
from ocx_indexbot.core.observe import Observation, observe_one_tag
from ocx_indexbot.core.policy import INDEX_POLICY_PATH
from ocx_indexbot.core.regenerate import regenerate
from ocx_indexbot.core.registry_checks import check_ownership
from ocx_indexbot.core.render import SourcePackage, build_render_plan
from ocx_indexbot.core.validate_entry import (
    ALWAYS_RESERVED_SEGMENTS,
    cas_relpath,
    check_name_matches_path,
    check_namespace_not_reserved,
    check_repository_allowlisted,
    serialize_package_root,
)
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode
from ocx_indexbot.model import (
    Owner,
    PackageId,
    PackageRoot,
    PullRequestInfo,
    TagEntry,
    Upstream,
    Yank,
)
from tests.fakes import FakeGitHub, FakeRegistry, FixedClock, InMemoryFiles, make_policy

# --- shared locations + builders (self-contained, DAMP) --------------------

_SRC = Path(__file__).resolve().parents[2] / "src" / "ocx_indexbot"


_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_TS = "2026-07-17T00:00:00Z"
_OWNER = Owner(login="alice", id=1)
_PKG = PackageId(segments=("kitware", "cmake"))


def _root(
    *,
    name: str = "ocx.sh/kitware/cmake",
    repository: str = "oci://ghcr.io/ocx-contrib/cmake",
    owners: tuple[Owner, ...] = (_OWNER,),
    status: str = "active",
    deprecated_message: str | None = None,
    created: str = "2026-07-17",
    desc: None = None,
    upstream: Upstream | None = None,
    superseded_by: str | None = None,
    tags: dict[str, TagEntry] | None = None,
) -> PackageRoot:
    return PackageRoot(
        name=name,
        repository=repository,
        owners=owners,
        status=status,  # type: ignore[arg-type]
        deprecated_message=deprecated_message,
        created=created,
        desc=desc,
        upstream=upstream,
        superseded_by=superseded_by,
        tags=dict(tags or {}),
    )


def _index_bytes(architecture: str = "amd64", digest: str = "sha256:" + "1" * 64) -> bytes:
    """An OCI image index as a registry would serve it — the exact bytes a
    CAS object under `o/sha256/` holds. Never re-serialized by the bot."""
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"platform": {"architecture": architecture, "os": "linux"}, "digest": digest}
            ],
        }
    ).encode("utf-8")


def _observation(tag: str, content_digest: str) -> Observation:
    return Observation(tag=tag, content_digest=content_digest, raw=_index_bytes())


# --- G-01 ------------------------------------------------------------------


# --- G-02 ------------------------------------------------------------------


def test_g02_name_must_equal_path() -> None:
    """G-02: `check_name_matches_path` raises when `name` != path-derived
    `ocx.sh/<ns>/<pkg>`, passes when it matches."""
    check_name_matches_path(
        _PKG, _root(name="ocx.sh/kitware/cmake"), index_name="ocx.sh"
    )  # matches -> no raise
    with pytest.raises(ValidationError):
        check_name_matches_path(_PKG, _root(name="ocx.sh/kitware/wrong"), index_name="ocx.sh")


# --- G-03 ------------------------------------------------------------------


def test_g03_repository_host_allowlisted() -> None:
    """G-03: `check_repository_allowlisted` raises for a host outside the
    deployment's policy, passes for one inside it."""
    hosts = frozenset({"ghcr.io", "ocx.sh"})
    check_repository_allowlisted("oci://ghcr.io/ocx-contrib/cmake", hosts)  # allowed -> no raise
    with pytest.raises(ValidationError):
        check_repository_allowlisted("oci://registry.evil.example/ocx-contrib/cmake", hosts)


# --- G-04 ------------------------------------------------------------------


def test_g04_new_package_is_human_lane() -> None:
    """G-04: a brand-new root (no base-ref file) classifies `new-package` —
    the human lane, never auto-merged."""
    assert classify_change(None, _root()) == "new-package"


# --- G-05 ------------------------------------------------------------------

_G05_MUTATIONS: list[tuple[str, Callable[[PackageRoot], PackageRoot]]] = [
    ("repository", lambda r: replace(r, repository="oci://ghcr.io/ocx-contrib/other")),
    ("owners", lambda r: replace(r, owners=(Owner(login="bob", id=2),))),
    ("status", lambda r: replace(r, status="deprecated")),
    ("deprecated_message", lambda r: replace(r, deprecated_message="gone")),
    ("superseded_by", lambda r: replace(r, superseded_by="kitware/cmake3")),
    (
        "yanked",
        lambda r: replace(
            r,
            tags={
                "1.0.0": replace(
                    r.tags["1.0.0"], yanked=Yank(reason="cve", at="2026-07-18T00:00:00Z")
                )
            },
        ),
    ),
]


@pytest.mark.parametrize(("key", "mutate"), _G05_MUTATIONS, ids=[k for k, _ in _G05_MUTATIONS])
def test_g05_governance_key_change_is_human_review(
    key: str, mutate: Callable[[PackageRoot], PackageRoot]
) -> None:
    """G-05 (Amendment A1 corrected key set): mutating any of
    `{repository, owners, status, deprecated_message, superseded_by, yanked}`
    forces `human-review-required`."""
    base = _root(tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS)})
    assert classify_change(base, mutate(base)) == "human-review-required", key


# --- G-06 ------------------------------------------------------------------


def test_g06_render_reachability_filter() -> None:
    """G-06: render copies only CAS reachable from a live tag; an orphaned
    CAS object (no live tag references its digest) is excluded."""
    live_obj = _index_bytes(architecture="amd64")
    orphan_obj = _index_bytes(architecture="arm64", digest="sha256:" + "2" * 64)
    root = _root(tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS)})
    source = SourcePackage(
        package_id=_PKG,
        root=root,
        root_raw=serialize_package_root(root),
        content_by_digest={
            f"{_DIGEST_A}.json": live_obj,
            f"{_DIGEST_B}.json": orphan_obj,
        },
    )
    paths = {fw.path for fw in build_render_plan((source,), name_segments=2)}
    assert cas_relpath(_PKG, _DIGEST_A, "json") in paths
    assert cas_relpath(_PKG, _DIGEST_B, "json") not in paths


# --- G-07 ------------------------------------------------------------------


def _seed_render_source(files: InMemoryFiles) -> None:
    root = _root(tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS)})
    files.write_bytes("p/kitware/cmake.json", serialize_package_root(root))
    files.write_bytes(
        f"p/kitware/cmake/o/sha256/{'a' * 64}.json",
        _index_bytes(),
    )


def test_g07_render_idempotent_noop() -> None:
    """G-07: `render --check` reports no drift against render's own prior
    output — the pipeline is deterministic (no wall clock)."""
    files = InMemoryFiles()
    _seed_render_source(files)
    write_args = argparse.Namespace(index_dir="", out="dist", check=False)
    assert cli_render.run(write_args, files=files, policy=make_policy()) == ExitCode.OK
    check_args = argparse.Namespace(index_dir="", out="dist", check=True)
    assert cli_render.run(check_args, files=files, policy=make_policy()) == ExitCode.OK


# --- G-08 (RETIRED — absence test) -----------------------------------------


def test_g08_no_repository_dispatch_surface() -> None:
    """G-08 RETIRED (ADR-4 Amendment A1 / ADR-6 FP-1): no `client_payload` /
    `PACKAGE_ID` reader anywhere in the package, no `core/validate_payload.py`,
    no `.github/workflows/announce.yml`.

    0.5.0 deleted `cli/announce.py` itself (`adr_forge_neutral_owners.md` D3),
    which used to be the one module this scanned. Widened to the whole package
    rather than dropped: the retired doorbell's shape is what must stay absent,
    and it could reappear in any module.
    """
    assert not (_SRC / "cli" / "announce.py").exists()
    assert not (_SRC / "core" / "validate_payload.py").exists()
    # The QUOTED spellings, not the bare words: a reader of either is a
    # subscript or an `os.environ` lookup, and both names still appear in
    # prose across the package explaining what was retired and why.
    for module in sorted(_SRC.rglob("*.py")):
        source = module.read_text(encoding="utf-8")
        for spelling in ('"client_payload"', "'client_payload'", '"PACKAGE_ID"', "'PACKAGE_ID'"):
            assert spelling not in source, f"{module}: {spelling}"


# --- G-09 ------------------------------------------------------------------


def test_g09_human_governed_fields_preserved() -> None:
    """G-09: `regenerate` carries every human-governed field verbatim from
    `current`; only `tags` (registry-derived) are rebuilt."""
    current = _root(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/ocx-contrib/cmake",
        owners=(_OWNER,),
        status="deprecated",
        deprecated_message="old",
        created="2026-01-01",
        upstream=Upstream(org="Kitware", repository_url="https://kitware.example", disclaimer="d"),
        superseded_by="kitware/cmake3",
        tags={"old": TagEntry(content=_DIGEST_A, observed=_TS)},
    )
    target = regenerate(current, (_observation("1.0.0", _DIGEST_B),), current.desc, FixedClock())
    for field in (
        "name",
        "repository",
        "owners",
        "status",
        "deprecated_message",
        "created",
        "upstream",
        "superseded_by",
    ):
        assert getattr(target, field) == getattr(current, field), field
    assert set(target.tags) == {"1.0.0"}  # tags regenerated from observations, not carried over


# --- ADR-2 ND-4 (reserved namespace segments) -------------------------------


def test_nd4_index_segment_is_reserved() -> None:
    """ADR-2 ND-4, WP-11 security review B-1a: `@ocx-sh/catalog`'s dogfood
    switchover made `index` a real, deployed top-level URL path
    (`dist/index/<label>/**`, the per-source wire mirror `mirror.ts`
    writes) -- a package claiming `p/index/<pkg>.json` would render to
    `dist/index/<pkg>.html` and collide with it. `index` was not previously
    in the control-path reservation group; this pins that it is, in both
    the namespace and package position `check_namespace_not_reserved`
    checks (a `PackageId` does not otherwise distinguish which position
    collided)."""
    assert "index" in ALWAYS_RESERVED_SEGMENTS
    with pytest.raises(ValidationError, match="index"):
        check_namespace_not_reserved(
            PackageId(segments=("index", "foo")),
            operator_reserved=frozenset(),
        )
    with pytest.raises(ValidationError, match="index"):
        check_namespace_not_reserved(
            PackageId(segments=("foo", "index")),
            operator_reserved=frozenset(),
        )


def test_nd4_c_segment_is_reserved() -> None:
    """ADR-2 ND-4, WP-11 security review S1: `/c/index.json` (the
    enumeration index, `adr_enumeration_index.md`) is a published top-level
    wire-contract URL -- a package claiming `p/c/<pkg>.json` or `p/<ns>/c.json`
    would sit beside it. `c` was the last deployed top-level control path not
    yet in the reservation group; this pins that it is, in both the
    namespace and package position `check_namespace_not_reserved` checks (a
    `PackageId` does not otherwise distinguish which position collided) --
    same convention as `test_nd4_index_segment_is_reserved` above."""
    assert "c" in ALWAYS_RESERVED_SEGMENTS
    with pytest.raises(ValidationError, match="c"):
        check_namespace_not_reserved(
            PackageId(segments=("c", "foo")),
            operator_reserved=frozenset(),
        )
    with pytest.raises(ValidationError, match="c"):
        check_namespace_not_reserved(
            PackageId(segments=("foo", "c")),
            operator_reserved=frozenset(),
        )


# --- G-10 ------------------------------------------------------------------


def test_g10_bounded_backoff_policy() -> None:
    """G-10: `is_retryable_status` is True for 429/5xx, False otherwise; a
    positive `retry_after` wins outright and the delay caps at
    `max_delay_seconds`."""
    for code in (429, 500, 503, 599):
        assert is_retryable_status(code) is True, code
    for code in (200, 400, 401, 404):
        assert is_retryable_status(code) is False, code
    policy = BackoffPolicy()
    assert delay_seconds(3, policy, jitter=0.0, retry_after=12.0) == 12.0
    assert delay_seconds(20, policy, jitter=0.5) == policy.max_delay_seconds


# --- G-11 ------------------------------------------------------------------


def test_g11_regenerate_idempotent() -> None:
    """G-11: regenerate -> diff twice; the second diff is empty and the
    `observed` timestamp does not churn even under a different clock."""
    raw = _index_bytes()
    content = "sha256:" + hashlib.sha256(raw).hexdigest()
    observations = (Observation(tag="1.0.0", content_digest=content, raw=raw),)
    current = _root()
    first = regenerate(
        current, observations, current.desc, FixedClock(fixed="2026-07-17T00:00:00Z")
    )
    second = regenerate(first, observations, first.desc, FixedClock(fixed="2026-09-09T09:09:09Z"))
    assert diff(_PKG, first, second, observations) is None
    assert second.tags["1.0.0"].observed == first.tags["1.0.0"].observed == "2026-07-17T00:00:00Z"


# --- G-12 ------------------------------------------------------------------


def test_g12_reconcile_is_verify_only_no_write() -> None:
    """G-12 (ADR-6 FP-3): `reconcile.run` on a clean tree writes nothing —
    no `FilePort` write, no anomaly issue — and exposes no `--dry-run`."""
    repository = "oci://ghcr.io/ocx-contrib/widget"
    manifest: dict[str, object] = {
        "schemaVersion": 2,
        "manifests": [
            {"platform": {"architecture": "amd64", "os": "linux"}, "digest": "sha256:" + "1" * 64}
        ],
    }
    registry = FakeRegistry(manifests={(repository, "1.0.0"): manifest})
    observation = observe_one_tag(repository, "1.0.0", registry)
    assert observation is not None
    content = observation.content_digest
    root = _root(
        name="ocx.sh/ocx-contrib/widget",
        repository=repository,
        tags={"1.0.0": TagEntry(content=content, observed=_TS)},
    )
    files = InMemoryFiles()
    files.write_bytes("p/ocx-contrib/widget.json", serialize_package_root(root))
    files.write_bytes(
        f"p/ocx-contrib/widget/o/sha256/{content.removeprefix('sha256:')}.json",
        observation.raw,
    )
    github = FakeGitHub()
    before = dict(files.files)

    result = reconcile.run(
        argparse.Namespace(package=None),
        files=files,
        registry=registry,
        github=github,
        policy=make_policy(registry_hosts=frozenset({"ghcr.io"})),
    )

    assert result == ExitCode.OK
    assert github.issues == {}  # no anomaly issue opened
    assert files.files == before  # FilePort store untouched — verify-only

    parser = argparse.ArgumentParser()
    reconcile.add_arguments(parser)
    with pytest.raises(SystemExit):  # no --dry-run arg exists
        parser.parse_args(["--dry-run"])


# --- G-13 ------------------------------------------------------------------


def test_g13_pinned_tag_mutation_is_anomaly() -> None:
    """G-13: `check_tag_mutations` flags a digest change on a build-pinned
    `X.Y.Z_<build>` tag; the rolling cascade targets a publish legitimately
    repoints — `latest` and the build-less `3.28.1` — are never flagged."""
    pinned = "3.28.1_20260216120000"
    committed = _root(tags={pinned: TagEntry(content=_DIGEST_A, observed=_TS)})
    findings = check_tag_mutations(_PKG, committed, (_observation(pinned, _DIGEST_B),))
    assert findings == (
        AnomalyFinding(
            package_id=_PKG, tag=pinned, committed_content=_DIGEST_A, fresh_content=_DIGEST_B
        ),
    )
    for tag in ("latest", "3.28.1"):
        floating = _root(tags={tag: TagEntry(content=_DIGEST_A, observed=_TS)})
        assert check_tag_mutations(_PKG, floating, (_observation(tag, _DIGEST_B),)) == ()


# --- G-14 ------------------------------------------------------------------

_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# --- G-15 ------------------------------------------------------------------


def test_g15_ownership_probe_dispositions() -> None:
    """G-15: the ownership probe surfaces three distinct dispositions —
    `mismatch` (block-tier, caller raises), `unconfirmed` (WARN, returned),
    `confirmed` (pass) — never collapsing one into another."""
    registry = FakeRegistry(
        ownership={
            "oci://ghcr.io/ocx-contrib/mismatch": "mismatch",
            "oci://ghcr.io/ocx-contrib/unconfirmed": "unconfirmed",
            "oci://ghcr.io/ocx-contrib/confirmed": "confirmed",
        }
    )
    assert (
        check_ownership("oci://ghcr.io/ocx-contrib/mismatch", "ocx.sh/x/a", registry) == "mismatch"
    )
    assert (
        check_ownership("oci://ghcr.io/ocx-contrib/unconfirmed", "ocx.sh/x/b", registry)
        == "unconfirmed"
    )
    assert (
        check_ownership("oci://ghcr.io/ocx-contrib/confirmed", "ocx.sh/x/c", registry)
        == "confirmed"
    )


# --- G-16 ------------------------------------------------------------------


# --- G-17 (RETIRED — absence test) -----------------------------------------


# --- G-18 (RETIRED — absence test) -----------------------------------------


def test_g18_no_reconcile_dry_run_gate() -> None:
    """G-18 RETIRED (ADR-6 FP-3): verify-only reconcile has no mutating mode
    to gate — `reconcile.py` exposes no `--dry-run` and reads no
    `RECONCILE_DRY_RUN`."""
    parser = argparse.ArgumentParser()
    reconcile.add_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--dry-run"])
    assert "RECONCILE_DRY_RUN" not in Path(reconcile.__file__).read_text(encoding="utf-8")


# --- G-19 ------------------------------------------------------------------


@pytest.fixture
def _github_output(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """`governance_check.run` unconditionally writes `disposition` to
    `$GITHUB_OUTPUT`; every invocation needs a target file for that write."""
    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    return output_file


def _refresh_pr_github(
    *,
    author_id: int,
    author_login: str = "alice",
    extra_changed_paths: tuple[str, ...] = (),
) -> FakeGitHub:
    """A refresh-class PR (only a tag's content changes) with `owners=(alice/1)`
    on both base and head; the author identity is the variable under test.

    `extra_changed_paths` are appended to the PR's changed-file list without
    any corresponding file content — `GitHubApi.get_pull_request_info` reads
    only `item["filename"]` from the files API, so a changed path carries no
    add/modify/delete status and an entry with no head content is exactly the
    shape a deletion arrives in."""
    root_path = "p/kitware/cmake.json"
    base = _root(tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS)})
    head = _root(tags={"1.0.0": TagEntry(content=_DIGEST_B, observed="2026-07-18T00:00:00Z")})
    files = {
        (root_path, "base-sha"): serialize_package_root(base),
        (root_path, "head-sha"): serialize_package_root(head),
        (
            ".github/maintainers.yml",
            "base-sha",
        ): b"maintainers:\n  - login: carol\n    id: 99\n",
    }
    info = PullRequestInfo(
        number=1,
        base_sha="base-sha",
        head_sha="head-sha",
        changed_paths=(root_path, *extra_changed_paths),
        author_login=author_login,
        author_id=author_id,
    )
    return FakeGitHub(files=files, pull_request_info={1: info})


def _disposition_of(github: FakeGitHub) -> str:
    """The commit-status state `governance_check.run` set — the exact value it
    also writes to `$GITHUB_OUTPUT` as `disposition`, which `governance.yml`'s
    `gh pr merge --auto --squash` step gates on (`== 'success'`)."""
    assert (
        governance_check.run(argparse.Namespace(pr_number=1), github=github, policy=make_policy())
        == ExitCode.OK
    )
    _context, state, _description = github.statuses["head-sha"][0]
    return state


def test_g19_machine_lane_requires_author_owner(_github_output: Path) -> None:
    """G-19: a refresh PR whose author's `github_id` is in the base-ref
    `owners[]` goes green (`success`); an author not in `owners[]` falls back
    to the human lane (`pending`)."""
    owner_github = _refresh_pr_github(author_id=1)
    assert (
        governance_check.run(
            argparse.Namespace(pr_number=1), github=owner_github, policy=make_policy()
        )
        == ExitCode.OK
    )
    _context, state, _description = owner_github.statuses["head-sha"][0]
    assert state == "success"

    stranger_github = _refresh_pr_github(author_id=999)
    assert (
        governance_check.run(
            argparse.Namespace(pr_number=1), github=stranger_github, policy=make_policy()
        )
        == ExitCode.OK
    )
    _context, stranger_state, description = stranger_github.statuses["head-sha"][0]
    assert stranger_state == "pending"
    assert "G-19" in description


# --- ADR-6 FP-5 (machine-lane scope; enforced in cli/classify_pr.py) --------

_OUT_OF_SCOPE_PATHS: list[tuple[str, str]] = [
    ("workflow", ".github/workflows/validate.yml"),
    ("bot-source", "bot/src/ocx_indexbot/cli/governance_check.py"),
    ("other-package-cas", f"p/acme/widget/o/sha256/{'c' * 64}.json"),
    ("other-package-root", "p/acme/widget.json"),
    ("deleted-repo-file", "README.md"),
    ("repo-config", ".github/maintainers.yml"),
    ("registry-host-policy", INDEX_POLICY_PATH),
]


@pytest.mark.parametrize(
    "extra_path",
    [path for _id, path in _OUT_OF_SCOPE_PATHS],
    ids=[case_id for case_id, _path in _OUT_OF_SCOPE_PATHS],
)
def test_fp5_machine_lane_rejects_out_of_scope_paths(extra_path: str, _github_output: Path) -> None:
    """ADR-6 FP-5: machine-lane content consists only of authorized package
    refreshes. A PR from a *legitimate owner* (author_id=1 is in the base-ref
    `owners[]`, so G-19 itself passes) whose refresh diff also touches any
    path outside the announce write-set must not reach the machine lane —
    the disposition stays `pending`, so `governance.yml`'s
    `gh pr merge --auto --squash` step never arms.

    Four of these cases have the scope gate as their *only* defense
    (`workflow`, `bot-source`, `other-package-cas`, `deleted-repo-file`,
    `repo-config`); `other-package-root` is defense-in-depth — an unowned
    root is separately caught by G-19, and a root absent at head by the
    deleted-root rule. Delete the scope gate and every other case
    auto-merges arbitrary repository content."""
    github = _refresh_pr_github(author_id=1, extra_changed_paths=(extra_path,))
    assert _disposition_of(github) == "pending"


def test_fp5_machine_lane_admits_the_announce_write_set(_github_output: Path) -> None:
    """The complementary half of FP-5 — the gate rejects out-of-scope paths
    without breaking the lane it exists to protect: the exact file set
    `cli/announce.py` writes (the root plus that same package's image index
    and readme/logo desc blobs) stays machine-lane (`success`)."""
    own_cas = (
        f"p/kitware/cmake/o/sha256/{'b' * 64}.json",  # image index
        f"p/kitware/cmake/o/sha256/{'c' * 64}.md",  # readme desc blob
        f"p/kitware/cmake/o/sha256/{'d' * 64}.svg",  # logo desc blob (svg)
        f"p/kitware/cmake/o/sha256/{'e' * 64}.png",  # logo desc blob (png)
    )
    github = _refresh_pr_github(author_id=1, extra_changed_paths=own_cas)
    assert _disposition_of(github) == "success"


# --- G-20 ------------------------------------------------------------------


def test_g20_human_lane_assigns_maintainers_reviewers(_github_output: Path) -> None:
    """G-20: a human-lane PR assigns reviewers from base-ref `maintainers.yml`
    minus the author, and posts one idempotent marker comment (a re-run
    updates in place rather than reposting)."""
    root_path = "p/kitware/cmake.json"
    base = _root(owners=(_OWNER,))
    head = _root(owners=(Owner(login="bob", id=2),))  # owners change -> human lane
    maintainers = b"maintainers:\n  - login: alice\n    id: 1\n  - login: carol\n    id: 99\n"
    files = {
        (root_path, "base-sha"): serialize_package_root(base),
        (root_path, "head-sha"): serialize_package_root(head),
        (".github/maintainers.yml", "base-sha"): maintainers,
    }
    info = PullRequestInfo(
        number=1,
        base_sha="base-sha",
        head_sha="head-sha",
        changed_paths=(root_path,),
        author_login="alice",
        author_id=1,
    )
    github = FakeGitHub(files=files, pull_request_info={1: info})

    governance_check.run(argparse.Namespace(pr_number=1), github=github, policy=make_policy())
    governance_check.run(argparse.Namespace(pr_number=1), github=github, policy=make_policy())

    assert github.requested_reviewers[1] == ["carol", "carol"]  # author 'alice' excluded both runs
    assert list(github.comments[1]) == [
        "<!-- indexbot:governance -->"
    ]  # one marker, updated in place
