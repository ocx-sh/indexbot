"""End-to-end verify-only `indexbot reconcile` through `main()` against a
fixture git tree plus both socket-level fakes (WP-B4, plan Phase 5).

Drives the REAL adapter stack — real `RegistryV2` (re-observing each committed
tag over a real socket) and real `GitHubApi` (the anomaly-issue write) — against
a canonical tree materialized by `build_git_tree`:

- A clean sweep (fake-GHCR serves exactly the image index the committed tree
  was seeded from) exits `ExitCode.OK` and performs ZERO forge writes:
  verify-only reconcile never touches `p/`, never files an issue when nothing
  is wrong — and stays clean over a repository that also carries the reserved
  tags `ocx package push` writes beside every version tag (ADR R3/D7).
- A pinned-tag digest mutation (fake-GHCR serves a *different* image index for
  the committed `1.0.0` tag) exits `ExitCode.ANOMALY`, files exactly one anomaly
  issue via `create_or_update_issue`, and writes NO `p/` tree — FP-3's
  verify-only, one-issue-on-anomaly contract. Because `reconcile.run` *raises*
  `AnomalyError` out through `main()`, this is also the flow that exercises
  B5's `$GITHUB_STEP_SUMMARY` observability floor (the structured failure reason
  main() emits on the workflow job summary) — asserted here, the one place among
  the four flows where main()'s raise-path actually engages.

X6 (both directions): the planted forge write credential (`GITHUB_TOKEN`) is
proven never to leak into a header the anonymous GHCR adapter sends — inspected
via an `httpx` request event hook threaded into the adapter's own client — and,
in reverse, the GHCR pull token is proven never to reach a forge request header,
inspected via the fake forge's own `received_headers` capture."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

import httpx

from indexbot.adapters.github_api import GitHubApi
from indexbot.adapters.registry_v2 import RegistryV2
from indexbot.cli.main import main
from indexbot.exit_codes import ExitCode
from tests.integration.fixtures.canonical import (
    CANONICAL_LEAF,
    CANONICAL_REPO_PATH,
    CANONICAL_ROLLING_TAG,
    CANONICAL_SPEC,
    CANONICAL_TAG,
    LEAF_BYTES,
    LEAF_DIGEST,
    ROLLING_SPEC,
    seed_registry,
)
from tests.integration.harness.git_tree import build_git_tree

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.integration.harness.fake_forge import FakeForgeServer
    from tests.integration.harness.fake_ghcr import FakeGhcrServer

_WIRING_REGISTRY = "indexbot.cli._wiring.RegistryV2"
_WIRING_GITHUB = "indexbot.cli._wiring.GitHubApi"
_REPOSITORY = "ocx-sh/index"
_FAKE_PULL_TOKEN = "fake-pull-token"  # noqa: S105
_FORGE_WRITE_SENTINEL = "ghp_forge_write_token_must_never_reach_ghcr"

# An image index that differs from `CANONICAL_INDEX` — its one descriptor
# declares `linux/arm64`. Re-observing a committed tag against this yields a
# different content digest than the tree was seeded with. On the build-pinned
# `CANONICAL_TAG` that is a force-repoint (anomaly); on `CANONICAL_ROLLING_TAG`
# it is what every legitimate republish does (clean).
_MUTATED_INDEX: dict[str, object] = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": LEAF_DIGEST,
            "size": len(LEAF_BYTES),
            "platform": {"architecture": "arm64", "os": "linux"},
        }
    ],
}

_DESC_TAG = "__ocx.desc"
_CANONICAL_TAG_FORM = f"sha256.{LEAF_DIGEST.removeprefix('sha256:')}"
"""The two reserved tag names `ocx package push` writes beside every version
tag (D7). Both resolve to bare image manifests, which `observe_one_tag`
refuses (D4(a)) — so a sweep that reached them at all would escalate."""


def _capturing_client(sink: list[dict[str, str]]) -> httpx.Client:
    def _record(request: httpx.Request) -> None:
        sink.append(dict(request.headers))

    return httpx.Client(event_hooks={"request": [_record]})


def _wire_adapters(
    monkeypatch: pytest.MonkeyPatch,
    index_tree: Path,
    ghcr: FakeGhcrServer,
    forge: FakeForgeServer,
    client: httpx.Client,
) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", str(index_tree))
    monkeypatch.setenv("GITHUB_REPOSITORY", _REPOSITORY)
    monkeypatch.setenv("GITHUB_TOKEN", _FORGE_WRITE_SENTINEL)
    monkeypatch.setattr(
        _WIRING_GITHUB,
        functools.partial(
            GitHubApi, base_url=forge.base_url, graphql_url=f"{forge.base_url}/graphql"
        ),
    )
    monkeypatch.setattr(
        _WIRING_REGISTRY, functools.partial(RegistryV2, base_url=ghcr.base_url, client=client)
    )


def _assert_no_ghcr_credential_leak(sent_headers: list[dict[str, str]]) -> None:
    assert sent_headers, "expected the GHCR adapter to have sent at least one request"
    for headers in sent_headers:
        authorization = headers.get("authorization")
        assert authorization in (None, f"Bearer {_FAKE_PULL_TOKEN}")
        assert not any(_FORGE_WRITE_SENTINEL in value for value in headers.values())


def test_clean_sweep_exits_ok_with_zero_forge_writes(
    fake_ghcr: FakeGhcrServer,
    fake_forge: FakeForgeServer,
    index_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_git_tree(index_tree, CANONICAL_SPEC)
    seed_registry(fake_ghcr)

    sent_headers: list[dict[str, str]] = []
    with _capturing_client(sent_headers) as client:
        _wire_adapters(monkeypatch, index_tree, fake_ghcr, fake_forge, client)
        exit_code = main(["reconcile"])

    assert exit_code == ExitCode.OK
    # Verify-only + clean: the forge was never touched at all.
    assert fake_forge.requests == []
    # The real token dance ran against GHCR, and no credential leaked to it.
    assert ("GET", "/token") in fake_ghcr.requests
    _assert_no_ghcr_credential_leak(sent_headers)


def test_pinned_tag_mutation_files_one_issue_and_engages_summary_floor(
    fake_ghcr: FakeGhcrServer,
    fake_forge: FakeForgeServer,
    index_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Seed the committed tree from CANONICAL_INDEX (content digest D1)...
    build_git_tree(index_tree, CANONICAL_SPEC)
    # ...but serve a DIFFERENT image index for the same pinned tag now (digest D2).
    seed_registry(fake_ghcr, _MUTATED_INDEX)

    issues_path = fake_forge.repo_path("issues")
    fake_forge.stub_json("GET", issues_path, [], params={"state": "open"})
    fake_forge.stub_json("POST", issues_path, {"number": 7}, status=201)

    summary_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    sent_headers: list[dict[str, str]] = []
    with _capturing_client(sent_headers) as client:
        _wire_adapters(monkeypatch, index_tree, fake_ghcr, fake_forge, client)
        exit_code = main(["reconcile"])

    assert exit_code == ExitCode.ANOMALY

    # Exactly one create_or_update_issue -> exactly one POST to the issues
    # collection; and verify-only means NO `p/` write (no Git Data API traffic).
    post_issue_calls = [
        path for method, path in fake_forge.requests if method == "POST" and path == issues_path
    ]
    assert len(post_issue_calls) == 1
    assert all("/git/" not in path for _method, path in fake_forge.requests)

    # B5's observability floor: main() caught the raised AnomalyError and wrote
    # a structured reason to the workflow job summary.
    summary = summary_path.read_text(encoding="utf-8")
    assert "## indexbot reconcile failed" in summary
    assert "pinned-tag-mutation" in summary

    _assert_no_ghcr_credential_leak(sent_headers)

    # X6 reverse direction: the GHCR pull token reaches no forge request header
    # (the forge saw the anomaly-issue GET+POST, carrying the forge token only).
    forge_headers = fake_forge.received_headers
    assert forge_headers, "expected the forge to have received the anomaly-issue calls"
    for headers in forge_headers:
        assert not any(_FAKE_PULL_TOKEN in value for value in headers.values())


def test_rolling_tag_repointed_by_a_legitimate_publish_files_nothing(
    fake_ghcr: FakeGhcrServer,
    fake_forge: FakeForgeServer,
    index_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false-positive direction, end to end.

    Same registry-side digest change as the test above, on a tag whose whole
    purpose is to move: `1.0.0` is the cascade alias the next
    `1.0.0_<build>` push repoints (`crates/ocx_lib/src/package/cascade.rs`).
    A sweep that alarms here files a tamper issue against a correct publish and
    reds the publisher's next mirror run, so this must exit OK with the forge
    untouched — and the forge is asserted untouched directly, not inferred, so
    a sweep that filed nothing because it swept nothing cannot pass.
    """
    build_git_tree(index_tree, ROLLING_SPEC)
    seed_registry(fake_ghcr, _MUTATED_INDEX, tag=CANONICAL_ROLLING_TAG)

    sent_headers: list[dict[str, str]] = []
    with _capturing_client(sent_headers) as client:
        _wire_adapters(monkeypatch, index_tree, fake_ghcr, fake_forge, client)
        exit_code = main(["reconcile"])

    assert exit_code == ExitCode.OK
    assert fake_forge.requests == []
    assert any(
        path.endswith(f"/manifests/{CANONICAL_ROLLING_TAG}") for _method, path in fake_ghcr.requests
    ), "the rolling tag was never observed — a clean exit here would prove nothing"


def test_reconcile_sweeps_a_repo_carrying_canonical_tags_clean(
    fake_ghcr: FakeGhcrServer,
    fake_forge: FakeForgeServer,
    index_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR R3: a real ocx-published repository carries more than version tags.

    `ocx package push` writes a canonical `sha256.<hex>` tag and a `__ocx.desc`
    tag beside every version tag, and both resolve to bare image manifests that
    `observe_one_tag` refuses outright. The sweep re-observes the tags the
    committed root *claims*, never the registry's tag list, so those reserved
    names must not enter the sweep at all — asserted directly against the
    fake's request log, not inferred from the exit code, since a sweep that did
    list tags would escalate `tag-unrecordable` and file an issue.
    """
    build_git_tree(index_tree, CANONICAL_SPEC)
    seed_registry(fake_ghcr)
    for reserved in (_CANONICAL_TAG_FORM, _DESC_TAG):
        fake_ghcr.add_manifest(CANONICAL_REPO_PATH, reserved, CANONICAL_LEAF)
    fake_ghcr.set_tags(CANONICAL_REPO_PATH, [CANONICAL_TAG, _CANONICAL_TAG_FORM, _DESC_TAG])

    sent_headers: list[dict[str, str]] = []
    with _capturing_client(sent_headers) as client:
        _wire_adapters(monkeypatch, index_tree, fake_ghcr, fake_forge, client)
        exit_code = main(["reconcile"])

    assert exit_code == ExitCode.OK
    assert fake_forge.requests == []

    served = fake_ghcr.requests
    assert not any(path.endswith("/tags/list") for _method, path in served)
    for reserved in (_CANONICAL_TAG_FORM, _DESC_TAG):
        assert not any(path.endswith(f"/manifests/{reserved}") for _method, path in served)
    # The claimed tag WAS swept, so the negatives above are about scope, not
    # about the sweep having quietly done nothing.
    assert any(path.endswith(f"/manifests/{CANONICAL_TAG}") for _method, path in served)
