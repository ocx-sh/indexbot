"""One named test per governance contract G-01..G-20 (spec X7, register §5).

Contract wording: `bot/CONTRACTS.md` §12, ADR-4
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
is the deliverable, not accidental drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from indexbot.cli import _wiring, announce, governance_check, reconcile
from indexbot.cli import render as cli_render
from indexbot.core.anomaly import AnomalyFinding, check_tag_mutations
from indexbot.core.backoff import BackoffPolicy, delay_seconds, is_retryable_status
from indexbot.core.diff import classify_change, diff
from indexbot.core.observe import Observation, observe_one_tag
from indexbot.core.policy import INDEX_POLICY_PATH, parse_index_policy
from indexbot.core.regenerate import regenerate
from indexbot.core.registry_checks import check_ownership
from indexbot.core.render import SourcePackage, build_render_plan
from indexbot.core.validate_entry import (
    cas_relpath,
    check_name_matches_path,
    check_repository_allowlisted,
    serialize_package_root,
)
from indexbot.errors import ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import (
    Owner,
    PackageId,
    PackageRoot,
    PullRequestInfo,
    TagEntry,
    Upstream,
    Yank,
)
from tests.fakes import FakeGitHub, FakeRegistry, FixedClock, InMemoryFiles

# --- shared locations + builders (self-contained, DAMP) --------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT_SCHEMA = _REPO_ROOT / "schema" / "root.schema.json"
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
_SRC = _REPO_ROOT / "bot" / "src" / "indexbot"


def _shipped_registry_hosts() -> frozenset[str]:
    """This repo's own committed `.github/index-policy.json`, parsed by the
    production parser — the public index's effective G-03 policy read from the
    real file, never a value restated here (a restated copy could agree with
    itself while the shipped file said something else)."""
    return parse_index_policy((_REPO_ROOT / INDEX_POLICY_PATH).read_bytes())


_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_TS = "2026-07-17T00:00:00Z"
_OWNER = Owner(github="alice", github_id=1)
_PKG = PackageId(namespace="kitware", package="cmake")


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


def _resolve_check_jsonschema() -> str:
    """Absolute path to the `check-jsonschema` console script the bot's own
    `task schema:validate` invokes (a dev dependency, `pyproject.toml`).

    Resolved beside the interpreter first (`uv run pytest` runs under the bot
    venv, whose `bin/` holds the script) so the invocation never depends on
    ambient `PATH`; falls back to `PATH` only if that layout differs. A hard
    error rather than a skip if truly absent — the audit artifact must run the
    real schema check, never silently pass on a missing tool."""
    candidate = Path(sys.executable).parent / "check-jsonschema"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("check-jsonschema")
    if found is None:  # pragma: no cover - environment guard, not a code path
        raise RuntimeError("check-jsonschema not found beside the interpreter or on PATH")
    return found


def _schema_accepts(root_json: str, tmp_path: Path) -> bool:
    """True iff `check-jsonschema` validates `root_json` against
    `schema/root.schema.json` — the exact schema layer the bot uses (never
    imported into `indexbot`; ADR-4 BD-1)."""
    fixture = tmp_path / "root.json"
    fixture.write_text(root_json, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - trusted local check-jsonschema, absolute path, no shell
        [_resolve_check_jsonschema(), "--schemafile", str(_ROOT_SCHEMA), str(fixture)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _valid_root_dict() -> dict[str, Any]:
    return {
        "name": "ocx.sh/kitware/cmake",
        "repository": "oci://ghcr.io/ocx-contrib/cmake",
        "owners": [{"github": "alice", "github_id": 1}],
        "status": "active",
        "deprecated_message": None,
        "created": "2026-07-17",
        "desc": None,
        "tags": {},
    }


# --- G-01 ------------------------------------------------------------------


def test_g01_schema_shape_rejects_malformed_root(tmp_path: Path) -> None:
    """G-01: `check-jsonschema`/`schema/root.schema.json` accepts a valid root
    and rejects one that violates the schema (a required key removed)."""
    valid = _valid_root_dict()
    assert _schema_accepts(json.dumps(valid), tmp_path)

    malformed = _valid_root_dict()
    del malformed["tags"]  # `tags` is a required property
    assert not _schema_accepts(json.dumps(malformed), tmp_path)


# --- G-02 ------------------------------------------------------------------


def test_g02_name_must_equal_path() -> None:
    """G-02: `check_name_matches_path` raises when `name` != path-derived
    `ocx.sh/<ns>/<pkg>`, passes when it matches."""
    check_name_matches_path(_PKG, _root(name="ocx.sh/kitware/cmake"))  # matches -> no raise
    with pytest.raises(ValidationError):
        check_name_matches_path(_PKG, _root(name="ocx.sh/kitware/wrong"))


# --- G-03 ------------------------------------------------------------------


def test_g03_repository_host_allowlisted() -> None:
    """G-03: `check_repository_allowlisted` raises for a host outside the
    deployment's policy, passes for one inside it."""
    hosts = _shipped_registry_hosts()
    check_repository_allowlisted("oci://ghcr.io/ocx-contrib/cmake", hosts)  # allowed -> no raise
    with pytest.raises(ValidationError):
        check_repository_allowlisted("oci://registry.evil.example/ocx-contrib/cmake", hosts)


def test_g03_shipped_policy_is_exactly_the_two_ocx_operated_hosts() -> None:
    """G-03's effective policy for THIS index — the committed
    `.github/index-policy.json`, parsed by the same code the bot runs.

    The allowlist became a per-deployment input; this repo IS the public
    index, and its policy is exactly `{"ghcr.io", "ocx.sh"}`: `ghcr.io` for
    every third-party mirror, `ocx.sh` for the operator's own first-party
    repositories (`ocx/cli`, `ocx/mirror`, `regclient/regsync`), which must
    have index roots or a default-index client 404s terminally on them. Any
    PR that widens the committed file further fails here, which is the
    reviewed-diff half of "extend only via reviewed PR" made mechanical."""
    assert _shipped_registry_hosts() == frozenset({"ghcr.io", "ocx.sh"})


def test_g03_shipped_policy_is_servable_by_an_adapter() -> None:
    """The other half of the same guard, asserted against the real file rather
    than a fabricated one: every host this index allowlists has a
    `RegistryPort` that can actually fetch its bytes (`cli/_wiring.py`)."""
    assert _shipped_registry_hosts() <= _wiring.REGISTRY_ADAPTER_HOSTS


# --- G-04 ------------------------------------------------------------------


def test_g04_new_package_is_human_lane() -> None:
    """G-04: a brand-new root (no base-ref file) classifies `new-package` —
    the human lane, never auto-merged."""
    assert classify_change(None, _root()) == "new-package"


# --- G-05 ------------------------------------------------------------------

_G05_MUTATIONS: list[tuple[str, Callable[[PackageRoot], PackageRoot]]] = [
    ("repository", lambda r: replace(r, repository="oci://ghcr.io/ocx-contrib/other")),
    ("owners", lambda r: replace(r, owners=(Owner(github="bob", github_id=2),))),
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
    paths = {fw.path for fw in build_render_plan((source,))}
    assert cas_relpath("kitware", "cmake", _DIGEST_A, "json") in paths
    assert cas_relpath("kitware", "cmake", _DIGEST_B, "json") not in paths


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
    assert cli_render.run(write_args, files=files) == ExitCode.OK
    check_args = argparse.Namespace(index_dir="", out="dist", check=True)
    assert cli_render.run(check_args, files=files) == ExitCode.OK


# --- G-08 (RETIRED — absence test) -----------------------------------------


def test_g08_no_repository_dispatch_surface() -> None:
    """G-08 RETIRED (ADR-4 Amendment A1 / ADR-6 FP-1): no `client_payload` /
    `PACKAGE_ID` reader in `announce.py`, no `core/validate_payload.py`, no
    `.github/workflows/announce.yml`."""
    announce_src = Path(announce.__file__).read_text(encoding="utf-8")
    assert "client_payload" not in announce_src
    assert "PACKAGE_ID" not in announce_src
    assert not (_SRC / "core" / "validate_payload.py").exists()
    assert not (_WORKFLOWS_DIR / "announce.yml").exists()


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
        allowed_hosts=_shipped_registry_hosts(),
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


def _workflow_files() -> list[Path]:
    return sorted(_WORKFLOWS_DIR.glob("*.yml"))


def _uses_refs(text: str) -> list[str]:
    return [match.group(1) for line in text.splitlines() if (match := _USES_RE.match(line))]


def test_g14_workflows_permissions_default_deny_and_sha_pinned() -> None:
    """G-14: every workflow has top-level `permissions: {}` and every
    marketplace `uses:` is pinned to a 40-hex commit SHA (local `./` composite
    actions are exempt — they are in-repo, not pinnable)."""
    workflows = _workflow_files()
    assert workflows, "expected at least one workflow to audit"
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert re.search(r"(?m)^permissions:\s*\{\}\s*$", text), (
            f"{workflow.name}: no top-level permissions: {{}}"
        )
        for ref in _uses_refs(text):
            if ref.startswith(("./", "docker://")):
                continue
            _, _, pin = ref.partition("@")
            assert _SHA_RE.fullmatch(pin), f"{workflow.name}: {ref!r} is not a 40-hex SHA pin"


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


def _validate_yml() -> str:
    return (_WORKFLOWS_DIR / "validate.yml").read_text(encoding="utf-8")


def _governance_yml() -> str:
    return (_WORKFLOWS_DIR / "governance.yml").read_text(encoding="utf-8")


def _job_block(text: str, job: str) -> str:
    """The YAML text of one job under `jobs:` — from its `  <job>:` header
    (exactly two-space indent) to the next two-space job header or EOF. A
    stdlib line scan, no YAML dependency in the credential process."""
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.rstrip() == f"  {job}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def test_g16_privileged_unprivileged_split() -> None:
    """G-16 (BD-5): `validate.yml`'s unprivileged PR-head job checks out the
    PR head and references no secrets; the privileged `governance-gate` job
    runs under `pull_request_target` — in its own workflow file, so neither
    trigger can emit a skipped check run under the other's context name —
    checks out the base ref only (no head checkout), and holds no PAT
    secret."""
    text = _validate_yml()
    unprivileged = _job_block(text, "schema-validate-pr")
    governance = _governance_yml()
    privileged = _job_block(governance, "governance-gate")

    # Unprivileged job: runs PR-head content, holds no secrets.
    assert re.search(r"(?m)^  pull_request:\s*$", text)
    assert not re.search(r"(?m)^  pull_request_target:\s*$", text)
    assert "github.event.pull_request.head.sha" in unprivileged
    assert "secrets." not in unprivileged

    # Privileged job: pull_request_target, base-ref checkout only, no PAT. The
    # absence of any `ref:` key is the real invariant — a checkout with no
    # `ref` defaults to the base branch tip, never PR head (the sole way to
    # check out head is an explicit `ref:` resolving `pull_request.head`).
    assert re.search(r"(?m)^  pull_request_target:\s*$", governance)
    assert not re.search(r"(?m)^  pull_request:\s*$", governance)
    assert "actions/checkout@" in privileged
    assert not re.search(r"(?m)^\s*ref:\s", privileged)
    assert "secrets." not in privileged


# --- G-17 (RETIRED — absence test) -----------------------------------------


def test_g17_no_announce_pat_surface() -> None:
    """G-17 RETIRED (ADR-4 Amendment A1 / ADR-6 FP-8): no namespace-scoped
    announce PAT in any workflow; the failed-check spam-label + stale-close
    path exists instead."""
    for workflow in _workflow_files():
        text = workflow.read_text(encoding="utf-8")
        assert "ANNOUNCE_PAT" not in text
        assert "secrets.ANNOUNCE" not in text
    assert (_WORKFLOWS_DIR / "pr-checks-label.yml").exists()  # FP-8 failed-check label path
    assert (_WORKFLOWS_DIR / "stale.yml").exists()  # FP-8 stale-close path


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
        ): b"maintainers:\n  - github: carol\n    github_id: 99\n",
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
    assert governance_check.run(argparse.Namespace(pr_number=1), github=github) == ExitCode.OK
    _context, state, _description = github.statuses["head-sha"][0]
    return state


def test_g19_machine_lane_requires_author_owner(_github_output: Path) -> None:
    """G-19: a refresh PR whose author's `github_id` is in the base-ref
    `owners[]` goes green (`success`); an author not in `owners[]` falls back
    to the human lane (`pending`)."""
    owner_github = _refresh_pr_github(author_id=1)
    assert governance_check.run(argparse.Namespace(pr_number=1), github=owner_github) == ExitCode.OK
    _context, state, _description = owner_github.statuses["head-sha"][0]
    assert state == "success"

    stranger_github = _refresh_pr_github(author_id=999)
    assert (
        governance_check.run(argparse.Namespace(pr_number=1), github=stranger_github) == ExitCode.OK
    )
    _context, stranger_state, description = stranger_github.statuses["head-sha"][0]
    assert stranger_state == "pending"
    assert "G-19" in description


# --- ADR-6 FP-5 (machine-lane scope; enforced in cli/classify_pr.py) --------

_OUT_OF_SCOPE_PATHS: list[tuple[str, str]] = [
    ("workflow", ".github/workflows/validate.yml"),
    ("bot-source", "bot/src/indexbot/cli/governance_check.py"),
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
    head = _root(owners=(Owner(github="bob", github_id=2),))  # owners change -> human lane
    maintainers = (
        b"maintainers:\n  - github: alice\n    github_id: 1\n  - github: carol\n    github_id: 99\n"
    )
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

    governance_check.run(argparse.Namespace(pr_number=1), github=github)
    governance_check.run(argparse.Namespace(pr_number=1), github=github)

    assert github.requested_reviewers[1] == ["carol", "carol"]  # author 'alice' excluded both runs
    assert list(github.comments[1]) == [
        "<!-- indexbot:governance -->"
    ]  # one marker, updated in place
