"""One named test per threat class (spec X7, register §5).

Adversarial tests: each mounts an attack against the *already-implemented*
bot and asserts the required fail-closed outcome. They pass against current
`src/indexbot` — they pin the defense, they do not add one. Threat framing:
ADR-6 (`adr_fork_pr_announce.md`) FP-1/FP-4/FP-5/FP-7 + ADR-4 BD-1/BD-4/BD-5.
Doubles come from `tests/fakes`; no sockets (B4 owns socket flows).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from indexbot.adapters.local_files import LocalFiles
from indexbot.cli import announce, governance_check
from indexbot.core.diff import classify_change, diff
from indexbot.core.observe import Observation
from indexbot.core.regenerate import regenerate
from indexbot.core.validate_entry import parse_digest, serialize_package_root
from indexbot.errors import ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import (
    ManifestFetch,
    ObservationObject,
    OciPlatform,
    Owner,
    OwnershipProbeResult,
    PackageId,
    PackageRoot,
    PlatformEntry,
    PullRequestInfo,
    TagEntry,
    Yank,
)
from tests.fakes import FakeGitHub, FixedClock, InMemoryFiles

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_TS = "2026-07-17T00:00:00Z"
_ROOT_PATH = "p/kitware/cmake.json"
_MAINTAINERS = b"maintainers:\n  - github: carol\n    github_id: 99\n"
_PKG = PackageId(namespace="kitware", package="cmake")


@pytest.fixture
def _github_output(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """`governance_check.run` always writes `disposition` to `$GITHUB_OUTPUT`;
    every invocation needs a target file for that write."""
    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    return output_file


def _root(
    *,
    name: str = "ocx.sh/kitware/cmake",
    repository: str = "oci://ghcr.io/ocx-contrib/cmake",
    owners: tuple[Owner, ...] = (Owner(github="alice", github_id=1),),
    tags: dict[str, TagEntry] | None = None,
) -> PackageRoot:
    return PackageRoot(
        name=name,
        repository=repository,
        owners=owners,
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags or {}),
    )


def _observation(tag: str, content_digest: str) -> Observation:
    return Observation(
        tag=tag,
        content_digest=content_digest,
        object=ObservationObject(
            platforms=(
                PlatformEntry(
                    platform=OciPlatform(architecture="amd64", os="linux"),
                    digest="sha256:" + "1" * 64,
                ),
            )
        ),
    )


# --- SSRF: host allowlist runs before any registry access ------------------


class _RaisingRegistry:
    """A `RegistryPort` whose every method fails the test if reached — proves
    `check_repository_allowlisted` runs BEFORE any `RegistryPort` call (ADR-4
    BD-1 SSRF ordering). Structurally conforms to the port (checked by the
    typed `announce.run` call below)."""

    def list_tags(self, repository: str) -> list[str]:
        raise AssertionError("registry reached before host-allowlist check (SSRF ordering broken)")

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        raise AssertionError("registry reached before host-allowlist check (SSRF ordering broken)")

    def get_desc_tag_digest(self, repository: str) -> str | None:
        raise AssertionError("registry reached before host-allowlist check (SSRF ordering broken)")

    def get_blob(self, repository: str, digest: str) -> bytes:
        raise AssertionError("registry reached before host-allowlist check (SSRF ordering broken)")

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        raise AssertionError("registry reached before host-allowlist check (SSRF ordering broken)")


def test_threat_ssrf_allowlist_before_registry() -> None:
    """`announce.run` on a committed root whose `repository` host is not
    allowlisted raises `ValidationError` from `check_repository_allowlisted`
    BEFORE any registry call — the raising registry double is never invoked."""
    hostile_root = _root(repository="oci://registry.evil.example/ocx-contrib/widget")
    index_github = FakeGitHub(
        files={("p/ns/pkg.json", "main"): serialize_package_root(hostile_root)}
    )
    args = argparse.Namespace(
        package="ns/pkg",
        tags="1.0.0",
        tags_file=None,
        out="dist",
        fork=None,
        index_repo="ocx-sh/index",
        yank=[],
        unyank=[],
        yank_reason="yanked via announce",
    )
    with pytest.raises(ValidationError):
        announce.run(
            args,
            registry=_RaisingRegistry(),
            index_github=index_github,
            fork_github=None,
            files=InMemoryFiles(),
            clock=FixedClock(),
            allowed_hosts=frozenset({"ghcr.io"}),
        )


# --- pull_request_target never checks out PR head --------------------------


def _job_block(text: str, job: str) -> str:
    """One job's YAML text, header (`  <job>:`, two-space indent) to the next
    two-space job header or EOF — stdlib line scan, no YAML dep."""
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.rstrip() == f"  {job}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def test_threat_pr_target_no_head_checkout() -> None:
    """Static parse of `validate.yml`: the `pull_request_target` governance
    job's `actions/checkout` carries no `ref:` (base-ref only) — it never
    resolves `github.event.pull_request.head`, so hostile PR-head content
    never runs in the credentialed job (ADR-6 FP-7)."""
    text = (_WORKFLOWS_DIR / "validate.yml").read_text(encoding="utf-8")
    privileged = _job_block(text, "governance-gate")
    assert "github.event_name == 'pull_request_target'" in privileged
    assert "actions/checkout@" in privileged
    # No `ref:` key at all — a checkout with no ref defaults to the base tip,
    # never PR head (the only way to reach head is an explicit `ref:` at
    # `pull_request.head`). The job's own comment documents this intent.
    assert not re.search(r"(?m)^\s*ref:\s", privileged)


# --- a PR cannot authorize itself by editing its own owners[] --------------


def test_threat_self_authorizing_owners_edit(_github_output: Path) -> None:
    """An attacker who adds themselves to `owners[]` in the PR head cannot
    self-authorize: G-19 reads the BASE root's `owners[]`, and the head edit
    is itself a G-05 change — so the PR is human-lane (`pending`), reviewers
    assigned."""
    legit = Owner(github="alice", github_id=1)
    attacker = Owner(github="mallory", github_id=999)
    base = _root(owners=(legit,), tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS)})
    head = _root(
        owners=(legit, attacker), tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS)}
    )

    # The head owners[] edit is itself a G-05 human-review change.
    assert classify_change(base, head) == "human-review-required"

    files = {
        (_ROOT_PATH, "base-sha"): serialize_package_root(base),
        (_ROOT_PATH, "head-sha"): serialize_package_root(head),
        (".github/maintainers.yml", "base-sha"): _MAINTAINERS,
    }
    info = PullRequestInfo(
        number=1,
        base_sha="base-sha",
        head_sha="head-sha",
        changed_paths=(_ROOT_PATH,),
        author_login="mallory",
        author_id=999,
    )
    github = FakeGitHub(files=files, pull_request_info={1: info})

    governance_check.run(argparse.Namespace(pr_number=1), github=github)

    _context, state, description = github.statuses["head-sha"][0]
    assert state == "pending"
    assert "human-review-required" in description
    assert github.requested_reviewers[1] == ["carol"]


# --- authorization binds the numeric id, not the login ---------------------


def _refresh_github(*, author_login: str, author_id: int) -> FakeGitHub:
    """A refresh PR (a tag's content changes) whose base+head `owners[]` are
    both `{github:'alice', github_id:1}`; only the author identity varies."""
    base = _root(tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS)})
    head = _root(tags={"1.0.0": TagEntry(content=_DIGEST_B, observed="2026-07-18T00:00:00Z")})
    files = {
        (_ROOT_PATH, "base-sha"): serialize_package_root(base),
        (_ROOT_PATH, "head-sha"): serialize_package_root(head),
        (".github/maintainers.yml", "base-sha"): _MAINTAINERS,
    }
    info = PullRequestInfo(
        number=1,
        base_sha="base-sha",
        head_sha="head-sha",
        changed_paths=(_ROOT_PATH,),
        author_login=author_login,
        author_id=author_id,
    )
    return FakeGitHub(files=files, pull_request_info={1: info})


def test_threat_authz_binds_numeric_id_not_login(_github_output: Path) -> None:
    """Base `owners[]` = `{github:'alice', github_id:1}`. A recycled login
    (`alice`/id 999) is NOT an owner -> `pending`; a renamed account keeping
    the id (`alice-renamed`/id 1) IS -> `success`. Authorization binds
    `github_id`, never the mutable login (ADR-6 FP-1/ND-8)."""
    recycled = _refresh_github(author_login="alice", author_id=999)
    assert governance_check.run(argparse.Namespace(pr_number=1), github=recycled) == ExitCode.OK
    _context, recycled_state, _description = recycled.statuses["head-sha"][0]
    assert recycled_state == "pending"

    renamed = _refresh_github(author_login="alice-renamed", author_id=1)
    assert governance_check.run(argparse.Namespace(pr_number=1), github=renamed) == ExitCode.OK
    _context, renamed_state, _description = renamed.statuses["head-sha"][0]
    assert renamed_state == "success"


# --- path escape + digest fullmatch reject before any filesystem/path join -


def test_threat_path_escape_rejected(tmp_path: Path) -> None:
    """`LocalFiles` rejects `..`-traversal and absolute paths before touching
    the filesystem; `parse_digest` rejects any non-`sha256:[a-f0-9]{64}`
    string before it can be joined into a CAS path (both raise)."""
    files = LocalFiles(tmp_path)
    with pytest.raises(ValidationError):
        files.read_text("../escape.txt")
    with pytest.raises(ValidationError):
        files.read_text("/etc/passwd")
    with pytest.raises(ValidationError):
        parse_digest("not-a-digest")
    with pytest.raises(ValidationError):
        parse_digest("sha256:" + "g" * 64)  # 'g' is not lowercase hex


# --- yank is grace, not delete; a yank mutation is human-gated -------------


def test_threat_yank_marker_survives_and_human_gated() -> None:
    """A committed `yanked` marker survives `regenerate` even when the tag's
    content changes (human-governed, G-05); introducing a `yanked` mutation
    in a PR routes to `human-review-required`."""
    yank = Yank(reason="cve", at="2026-07-18T00:00:00Z")
    current = _root(tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS, yanked=yank)})
    target = regenerate(current, (_observation("1.0.0", _DIGEST_B),), current.desc, FixedClock())
    assert target.tags["1.0.0"].yanked == yank  # survives regeneration
    assert target.tags["1.0.0"].content == _DIGEST_B  # content still refreshed

    before = _root(tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS)})
    after = _root(tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS, yanked=yank)})
    assert classify_change(before, after) == "human-review-required"


# --- an empty (byte-identical) diff produces no merge content --------------


def test_threat_empty_diff_pr_no_merge() -> None:
    """A byte-identical no-op root diff yields no `Patch` (`diff` returns
    `None`) — nothing for the machine lane to merge. Classification alone
    still labels an identical-root PR `refresh`; the seam between "refresh
    class" and "no patch to write" is what WP-B9 optionally hardens."""
    root = _root(tags={"1.0.0": TagEntry(content=_DIGEST_A, observed=_TS)})
    assert diff(_PKG, root, root, (_observation("1.0.0", _DIGEST_A),)) is None
    assert classify_change(root, root) == "refresh"
