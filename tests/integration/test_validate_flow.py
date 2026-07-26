"""End-to-end `indexbot validate` through `main()` against a fixture git tree
plus the socket-level `FakeGhcrServer` (WP-B4, plan Phase 5).

A tier distinct from `tests/cli/test_validate.py`'s pure-function/fake-double
unit tests: every flow here drives the REAL adapter stack — real argparse ->
real `cli/_wiring` -> real `GhcrRegistry` HTTP adapter (anonymous bearer-token
dance included) -> real `LocalFiles` — against a canonical tree materialized by
`build_git_tree` and manifests served over a real socket by `fake_ghcr`.

Exit codes are asserted against the `ExitCode` enum, never a hardcoded int.
Note the true dispositions the code produces (see `exit_codes.py`,
`core/validate_entry.py`):

- A tampered committed CAS object's bytes trip
  `check_digest_self_consistent`, which raises `AnomalyError` ->
  `ExitCode.ANOMALY`. (The Phase-5 plan sketch labelled this
  `VALIDATION_FAILURE`; the committed code's actual disposition is the
  stricter ANOMALY, asserted here.)
- Non-canonical committed root bytes trip the byte-exact discipline
  (`ValidationError`) -> `ExitCode.VALIDATION_FAILURE` (FP-4).

`cli/validate.py` aggregates each file's error into a returned `ExitCode` and
never *raises* an `IndexBotError` out of `run`, so `cli/main.py`'s
`$GITHUB_STEP_SUMMARY` observability floor (B5) is never engaged by a validate
failure — asserted negatively below, and exercised positively instead by the
reconcile anomaly flow (`test_reconcile_verify_flow.py`), the one flow that
does raise through `main()`.
"""

from __future__ import annotations

import dataclasses
import functools
import json
from typing import TYPE_CHECKING

import httpx

from indexbot.adapters.ghcr import GhcrRegistry
from indexbot.cli.main import main
from indexbot.core.validate_entry import parse_package_root, serialize_package_root
from indexbot.exit_codes import ExitCode
from tests.integration.fixtures.canonical import (
    CANONICAL_ROOT_PATH,
    CANONICAL_SPEC,
    CANONICAL_TAG,
    LEAF_BYTES,
    LEAF_DIGEST,
    seed_registry,
)
from tests.integration.harness.git_tree import build_git_tree

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.integration.harness.fake_ghcr import FakeGhcrServer

_WIRING_GHCR = "indexbot.cli._wiring.GhcrRegistry"
_FAKE_PULL_TOKEN = "fake-pull-token"  # noqa: S105
"""The bearer `FakeGhcrServer`'s `/token` endpoint issues — the ONLY credential
the anonymous registry adapter is ever supposed to carry to a `/v2/*` endpoint."""
_FORGE_WRITE_SENTINEL = "ghp_forge_write_token_must_never_reach_ghcr"
"""A stand-in forge write credential planted in the environment: X6 proves it
never lands in a header the GHCR adapter sends."""


def _capturing_client(sink: list[dict[str, str]]) -> httpx.Client:
    """An `httpx.Client` whose request event hook records the exact headers of
    every outbound request into `sink` — the wire-level seam this WP uses to
    inspect what the fake registry receives (the frozen B1 harness records only
    `(method, path)`, not headers; see the module report's deferred finding)."""

    def _record(request: httpx.Request) -> None:
        sink.append(dict(request.headers))

    return httpx.Client(event_hooks={"request": [_record]})


def _seed_workspace(index_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_git_tree(index_tree, CANONICAL_SPEC)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(index_tree))
    # A planted forge write credential that validate has no legitimate reason
    # to send anywhere — X6 asserts it never leaks into a GHCR-bound header.
    monkeypatch.setenv("GITHUB_TOKEN", _FORGE_WRITE_SENTINEL)


def _cas_object_path(index_tree: Path) -> Path:
    cas_dir = index_tree / "p" / "acme" / "widget" / "o" / "sha256"
    objects = sorted(cas_dir.glob("*.json"))
    assert len(objects) == 1, f"expected exactly one seeded CAS object, found {objects}"
    return objects[0]


def test_validate_clean_tree_exits_ok_with_no_credential_leak(
    fake_ghcr: FakeGhcrServer, index_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_workspace(index_tree, monkeypatch)
    seed_registry(fake_ghcr)

    sent_headers: list[dict[str, str]] = []
    with _capturing_client(sent_headers) as client:
        monkeypatch.setattr(
            _WIRING_GHCR,
            functools.partial(GhcrRegistry, base_url=fake_ghcr.base_url, client=client),
        )
        exit_code = main(["validate", CANONICAL_ROOT_PATH])

    assert exit_code == ExitCode.OK

    # The real anonymous bearer-token dance ran against the fake (an initial
    # unauthenticated /v2/ request 401'd, the adapter fetched a pull token).
    served = fake_ghcr.requests
    assert ("GET", "/token") in served
    assert any(path.endswith(f"/manifests/{CANONICAL_TAG}") for _method, path in served)

    # X6 (mirrored index-side): no surprise credential reached GHCR. The only
    # Authorization the adapter ever sends is the fake's own issued pull token
    # (or none at all, on the anonymous first attempt / the /token fetch); the
    # planted forge write credential appears in NO header the registry received.
    assert sent_headers, "expected the GHCR adapter to have sent at least one request"
    for headers in sent_headers:
        authorization = headers.get("authorization")
        assert authorization in (None, f"Bearer {_FAKE_PULL_TOKEN}")
        assert not any(_FORGE_WRITE_SENTINEL in value for value in headers.values())


def test_validate_tampered_cas_object_is_anomaly_without_summary_floor(
    fake_ghcr: FakeGhcrServer, index_tree: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_workspace(index_tree, monkeypatch)
    seed_registry(fake_ghcr)
    monkeypatch.setattr(_WIRING_GHCR, functools.partial(GhcrRegistry, base_url=fake_ghcr.base_url))

    # Corrupt the committed CAS object's bytes in place (its filename digest is
    # left untouched, so this is a self-consistency violation, not a dangling
    # reference): the file's name now lies about its own content.
    _cas_object_path(index_tree).write_bytes(b"tampered CAS bytes")

    # B5's observability floor writes here only when main() catches a raised
    # IndexBotError; validate returns its ExitCode instead of raising, so the
    # file must stay absent — pinning that validate is not on the raise path.
    summary_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    exit_code = main(["validate", CANONICAL_ROOT_PATH])

    # check_digest_self_consistent raises AnomalyError (a CAS object whose bytes
    # do not hash to its own digest is corruption, never a routine PR mistake).
    assert exit_code == ExitCode.ANOMALY
    assert not summary_path.exists()


def test_validate_non_canonical_root_bytes_is_validation_failure(
    fake_ghcr: FakeGhcrServer, index_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_workspace(index_tree, monkeypatch)
    seed_registry(fake_ghcr)
    monkeypatch.setattr(_WIRING_GHCR, functools.partial(GhcrRegistry, base_url=fake_ghcr.base_url))

    # Re-emit the committed root as compact JSON: it still parses to the exact
    # same PackageRoot, but its bytes are not the canonical pretty-printed
    # serialization CI re-derives and byte-compares against (FP-4 byte-exact
    # discipline).
    root_path = index_tree / CANONICAL_ROOT_PATH
    canonical_bytes = root_path.read_bytes()
    assert serialize_package_root(parse_package_root(canonical_bytes)) == canonical_bytes
    non_canonical = json.dumps(json.loads(canonical_bytes)).encode("utf-8")
    assert non_canonical != canonical_bytes
    root_path.write_bytes(non_canonical)

    exit_code = main(["validate", CANONICAL_ROOT_PATH])

    assert exit_code == ExitCode.VALIDATION_FAILURE


def test_validate_rejects_a_tag_whose_cas_object_is_not_an_image_index(
    fake_ghcr: FakeGhcrServer,
    index_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D4(c) end to end: `o/` holds OCI image indices, and nothing else.

    A bare image manifest is exactly what a publisher repointing a tag at a
    single-platform push would leave behind. The committed object's filename is
    still its own sha256 and the root's bytes are still the canonical
    serialization, so neither the CAS self-consistency check nor the byte-exact
    discipline is what fires here.

    The exit code alone would not pin the document-kind gate: a bare manifest in
    `o/` also breaks claim verification against the index the registry still
    serves at this tag, which reports the same `VALIDATION_FAILURE`. So the
    rejection *reason* is asserted on stderr — delete or reorder the D4(c) check
    and this fails, rather than passing on the second failure path.
    """
    _seed_workspace(index_tree, monkeypatch)
    seed_registry(fake_ghcr)
    monkeypatch.setattr(_WIRING_GHCR, functools.partial(GhcrRegistry, base_url=fake_ghcr.base_url))

    cas_dir = _cas_object_path(index_tree).parent
    _cas_object_path(index_tree).unlink()
    (cas_dir / f"{LEAF_DIGEST.removeprefix('sha256:')}.json").write_bytes(LEAF_BYTES)

    root_path = index_tree / CANONICAL_ROOT_PATH
    root = parse_package_root(root_path.read_bytes())
    repointed = dataclasses.replace(
        root,
        tags={CANONICAL_TAG: dataclasses.replace(root.tags[CANONICAL_TAG], content=LEAF_DIGEST)},
    )
    root_path.write_bytes(serialize_package_root(repointed))

    exit_code = main(["validate", CANONICAL_ROOT_PATH])

    assert exit_code == ExitCode.VALIDATION_FAILURE
    assert "not an OCI image index" in capsys.readouterr().err
