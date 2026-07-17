"""Tests for `adapters/ghcr.py` — CONTRACTS.md §9.

One `respx.mock`-decorated test per distinct response class
(200/404/401-then-retry/429-with-and-without-Retry-After/5xx-exhausted/
malformed-JSON/pagination/page-bound-exceeded), per CONTRACTS.md §2's test
conventions. The 401/429/5xx retry machinery lives in `GhcrRegistry._send`,
shared by every public method — those response classes are exercised once
(via `get_manifest`) rather than duplicated on every method, per
`quality-core.md`'s DRY guidance; each method's *own* distinct behavior
(404 -> `[]`/`KeyError`/`None`) is tested directly on that method.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx

from indexbot.adapters import ghcr
from indexbot.adapters.ghcr import GhcrRegistry
from indexbot.errors import AnomalyError, TransientError
from indexbot.ports import RegistryPort

_BASE = "https://ghcr.io"
_REPO_PATH = "ocx-contrib/cmake"
_REPOSITORY = f"oci://ghcr.io/{_REPO_PATH}"
_TAGS_URL = f"{_BASE}/v2/{_REPO_PATH}/tags/list"
_DESC_URL = f"{_BASE}/v2/{_REPO_PATH}/manifests/__ocx.desc"


def _no_sleep(seconds: float) -> None:
    del seconds  # test double — skip the real backoff wait


def test_ghcr_registry_conforms_to_registry_port() -> None:
    registry: RegistryPort = GhcrRegistry()
    assert isinstance(registry, GhcrRegistry)


# --- list_tags ---------------------------------------------------------


@respx.mock
def test_list_tags_single_page() -> None:
    respx.get(_TAGS_URL, params={"n": "100"}).mock(
        return_value=httpx.Response(200, json={"tags": ["latest", "1.0.0"]})
    )
    registry = GhcrRegistry()
    assert registry.list_tags(_REPOSITORY) == ["latest", "1.0.0"]


@respx.mock
def test_list_tags_404_returns_empty_list() -> None:
    respx.get(_TAGS_URL, params={"n": "100"}).mock(return_value=httpx.Response(404))
    registry = GhcrRegistry()
    assert registry.list_tags(_REPOSITORY) == []


@respx.mock
def test_list_tags_paginates_across_link_header() -> None:
    # respx's `params=` matcher is a subset match (a route with `n=100` also
    # matches a request whose query is `n=100&last=...`) — the more specific
    # page-2 route must be registered first so first-match-wins picks it.
    page2_url = f"{_TAGS_URL}?n=100&last=1.1.0"
    respx.get(page2_url).mock(return_value=httpx.Response(200, json={"tags": ["2.0.0"]}))
    respx.get(_TAGS_URL, params={"n": "100"}).mock(
        return_value=httpx.Response(
            200,
            json={"tags": ["1.0.0", "1.1.0"]},
            headers={"Link": f'<{page2_url}>; rel="next"'},
        )
    )
    registry = GhcrRegistry()
    assert registry.list_tags(_REPOSITORY) == ["1.0.0", "1.1.0", "2.0.0"]


@respx.mock
def test_list_tags_page_bound_exceeded_raises_transient() -> None:
    # A next-link chain that never terminates — the hard page cap is what
    # stops this from looping forever, not a well-behaved server.
    looping_url = f"{_TAGS_URL}?n=100&last=1.0.0"
    respx.get(_TAGS_URL, params={"n": "100"}).mock(
        return_value=httpx.Response(
            200, json={"tags": ["1.0.0"]}, headers={"Link": f'<{looping_url}>; rel="next"'}
        )
    )
    respx.get(looping_url).mock(
        return_value=httpx.Response(
            200, json={"tags": ["1.0.0"]}, headers={"Link": f'<{looping_url}>; rel="next"'}
        )
    )
    registry = GhcrRegistry(max_pages=2)
    with pytest.raises(TransientError, match="pagination exceeded"):
        registry.list_tags(_REPOSITORY)


@respx.mock
def test_list_tags_skips_malformed_link_segment_before_a_valid_next() -> None:
    page2_url = f"{_TAGS_URL}?n=100&last=1.0.0"
    respx.get(page2_url).mock(return_value=httpx.Response(200, json={"tags": ["2.0.0"]}))
    respx.get(_TAGS_URL, params={"n": "100"}).mock(
        return_value=httpx.Response(
            200,
            json={"tags": ["1.0.0"]},
            # A malformed segment (no "<...>" target) ahead of the real one —
            # the parser must skip it, not stop pagination early.
            headers={"Link": f'not-a-valid-target; rel="next", <{page2_url}>; rel="next"'},
        )
    )
    registry = GhcrRegistry()
    assert registry.list_tags(_REPOSITORY) == ["1.0.0", "2.0.0"]


@respx.mock
def test_list_tags_paginates_across_relative_link_header() -> None:
    # A relative `Link` target (no scheme/host) is resolved against
    # `base_url` — distinct from the absolute-URL pagination case above.
    respx.get(f"{_TAGS_URL}?n=100&last=1.0.0").mock(
        return_value=httpx.Response(200, json={"tags": ["2.0.0"]})
    )
    respx.get(_TAGS_URL, params={"n": "100"}).mock(
        return_value=httpx.Response(
            200,
            json={"tags": ["1.0.0"]},
            headers={"Link": f'</v2/{_REPO_PATH}/tags/list?n=100&last=1.0.0>; rel="next"'},
        )
    )
    registry = GhcrRegistry()
    assert registry.list_tags(_REPOSITORY) == ["1.0.0", "2.0.0"]


@respx.mock
def test_list_tags_rejects_cross_host_next_link() -> None:
    # A `Link: rel="next"` target on a different host must never be
    # followed — this adapter's cached bearer pull-token would otherwise be
    # replayed against an attacker-controlled origin (SSRF-via-pagination).
    evil_url = "https://evil.example.com/v2/ocx-contrib/cmake/tags/list?n=100&last=1.0.0"
    respx.get(_TAGS_URL, params={"n": "100"}).mock(
        return_value=httpx.Response(
            200, json={"tags": ["1.0.0"]}, headers={"Link": f'<{evil_url}>; rel="next"'}
        )
    )
    registry = GhcrRegistry()
    with pytest.raises(ValueError, match="does not match"):
        registry.list_tags(_REPOSITORY)


@respx.mock
def test_list_tags_stops_when_link_header_has_no_rel_next() -> None:
    respx.get(_TAGS_URL, params={"n": "100"}).mock(
        return_value=httpx.Response(
            200,
            json={"tags": ["1.0.0"]},
            headers={"Link": f'<{_TAGS_URL}?n=100&last=0.9.0>; rel="prev"'},
        )
    )
    registry = GhcrRegistry()
    assert registry.list_tags(_REPOSITORY) == ["1.0.0"]


# --- get_manifest (also covers the shared 401/429/5xx retry machinery) --


@respx.mock
def test_get_manifest_returns_parsed_json() -> None:
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(
            200, json={"mediaType": "application/vnd.oci.image.manifest.v1+json"}
        )
    )
    registry = GhcrRegistry()
    fetch = registry.get_manifest(_REPOSITORY, "v1.0.0")
    assert fetch.parsed["mediaType"] == "application/vnd.oci.image.manifest.v1+json"


@respx.mock
def test_get_manifest_digest_is_computed_from_response_body() -> None:
    body = b'{"mediaType": "application/vnd.oci.image.manifest.v1+json"}'
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(200, content=body)
    )
    registry = GhcrRegistry()
    fetch = registry.get_manifest(_REPOSITORY, "v1.0.0")
    assert fetch.raw == body
    assert fetch.digest == f"sha256:{hashlib.sha256(body).hexdigest()}"


@respx.mock
def test_get_manifest_matching_header_digest_passes_through() -> None:
    body = b'{"mediaType": "application/vnd.oci.image.manifest.v1+json"}'
    digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(200, content=body, headers={"Docker-Content-Digest": digest})
    )
    registry = GhcrRegistry()
    fetch = registry.get_manifest(_REPOSITORY, "v1.0.0")
    assert fetch.digest == digest


@respx.mock
def test_get_manifest_mismatched_header_digest_raises_anomaly() -> None:
    # The header claims a digest that does not match the body actually
    # served — never trusted verbatim (ports.py's digest doctrine).
    body = b'{"mediaType": "application/vnd.oci.image.manifest.v1+json"}'
    wrong_digest = "sha256:" + "0" * 64
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(
            200, content=body, headers={"Docker-Content-Digest": wrong_digest}
        )
    )
    registry = GhcrRegistry()
    with pytest.raises(AnomalyError, match="digest mismatch"):
        registry.get_manifest(_REPOSITORY, "v1.0.0")


@respx.mock
def test_get_manifest_404_raises_keyerror() -> None:
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/missing").mock(return_value=httpx.Response(404))
    registry = GhcrRegistry()
    with pytest.raises(KeyError):
        registry.get_manifest(_REPOSITORY, "missing")


@respx.mock
def test_get_manifest_malformed_json_raises_value_error() -> None:
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(200, content=b"not-json{")
    )
    registry = GhcrRegistry()
    with pytest.raises(ValueError):
        registry.get_manifest(_REPOSITORY, "v1.0.0")


@respx.mock
def test_get_manifest_401_then_token_retry_succeeds() -> None:
    token_route = respx.get(f"{_BASE}/token").mock(
        return_value=httpx.Response(200, json={"token": "tok-123"})
    )
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": True})]
    )
    registry = GhcrRegistry()
    # The retried request only succeeds because a token was fetched and
    # attached — the second manifest response is only reached via that path.
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    assert token_route.call_count == 1


@respx.mock
def test_get_manifest_persistent_401_raises_transient() -> None:
    respx.get(f"{_BASE}/token").mock(return_value=httpx.Response(200, json={"token": "tok-1"}))
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(return_value=httpx.Response(401))
    registry = GhcrRegistry()
    with pytest.raises(TransientError, match="persistent 401"):
        registry.get_manifest(_REPOSITORY, "v1.0.0")


@respx.mock
def test_token_cached_across_calls_on_same_repository() -> None:
    token_route = respx.get(f"{_BASE}/token").mock(
        return_value=httpx.Response(200, json={"token": "tok-1"})
    )
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"n": 1}),
            httpx.Response(200, json={"n": 2}),
        ]
    )
    registry = GhcrRegistry()
    registry.get_manifest(_REPOSITORY, "v1.0.0")
    registry.get_manifest(_REPOSITORY, "v1.0.0")
    assert token_route.call_count == 1


@respx.mock
def test_429_without_retry_after_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ghcr.time, "sleep", _no_sleep)
    route = respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": True})]
    )
    registry = GhcrRegistry()
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    assert route.call_count == 2


@respx.mock
def test_429_with_retry_after_uses_server_value(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(ghcr.time, "sleep", sleeps.append)
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    registry = GhcrRegistry()
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    # Retry-After wins outright over the exponential/jitter formula (G-10).
    assert sleeps == [2.0]


@respx.mock
def test_429_with_unparseable_retry_after_falls_back_to_exponential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(ghcr.time, "sleep", sleeps.append)
    monkeypatch.setattr(ghcr.random, "random", lambda: 0.5)
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "not-a-number"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    registry = GhcrRegistry()
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    # base_delay_seconds(1.0) * 2**0 * (0.5 + jitter(0.5)) == 1.0
    assert sleeps == [1.0]


@respx.mock
def test_429_with_non_positive_retry_after_falls_back_to_exponential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(ghcr.time, "sleep", sleeps.append)
    monkeypatch.setattr(ghcr.random, "random", lambda: 0.5)
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    registry = GhcrRegistry()
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    assert sleeps == [1.0]


@respx.mock
def test_5xx_exhausted_raises_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ghcr.time, "sleep", _no_sleep)
    route = respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(503)
    )
    registry = GhcrRegistry()
    with pytest.raises(TransientError, match="backoff exhausted"):
        registry.get_manifest(_REPOSITORY, "v1.0.0")
    assert route.call_count == 5  # BackoffPolicy() default max_attempts


# --- get_desc_tag_digest -------------------------------------------------


@respx.mock
def test_get_desc_tag_digest_present() -> None:
    digest = "sha256:" + "a" * 64
    respx.head(_DESC_URL).mock(
        return_value=httpx.Response(200, headers={"Docker-Content-Digest": digest})
    )
    registry = GhcrRegistry()
    assert registry.get_desc_tag_digest(_REPOSITORY) == digest


@respx.mock
def test_get_desc_tag_digest_absent_returns_none() -> None:
    respx.head(_DESC_URL).mock(return_value=httpx.Response(404))
    registry = GhcrRegistry()
    assert registry.get_desc_tag_digest(_REPOSITORY) is None


# --- get_blob ------------------------------------------------------------


@respx.mock
def test_get_blob_returns_bytes() -> None:
    digest = "sha256:" + "b" * 64
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/blobs/{digest}").mock(
        return_value=httpx.Response(200, content=b"# Readme")
    )
    registry = GhcrRegistry()
    assert registry.get_blob(_REPOSITORY, digest) == b"# Readme"


@respx.mock
def test_get_blob_missing_raises_keyerror() -> None:
    digest = "sha256:" + "c" * 64
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/blobs/{digest}").mock(return_value=httpx.Response(404))
    registry = GhcrRegistry()
    with pytest.raises(KeyError):
        registry.get_blob(_REPOSITORY, digest)


# --- probe_ownership -------------------------------------------------------


@respx.mock
def test_probe_ownership_unconfirmed_when_no_desc_tag() -> None:
    respx.head(_DESC_URL).mock(return_value=httpx.Response(404))
    registry = GhcrRegistry()
    assert registry.probe_ownership(_REPOSITORY, "ocx.sh/kitware/cmake") == "unconfirmed"


@respx.mock
def test_probe_ownership_unconfirmed_when_annotation_missing() -> None:
    digest = "sha256:" + "d" * 64
    respx.head(_DESC_URL).mock(
        return_value=httpx.Response(200, headers={"Docker-Content-Digest": digest})
    )
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/{digest}").mock(
        return_value=httpx.Response(200, json={"annotations": {}})
    )
    registry = GhcrRegistry()
    assert registry.probe_ownership(_REPOSITORY, "ocx.sh/kitware/cmake") == "unconfirmed"


@respx.mock
def test_probe_ownership_unconfirmed_when_annotations_key_absent() -> None:
    # Distinct from the "present but empty" case above — no `annotations`
    # key at all in the manifest, not merely an empty one.
    digest = "sha256:" + "9" * 64
    respx.head(_DESC_URL).mock(
        return_value=httpx.Response(200, headers={"Docker-Content-Digest": digest})
    )
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/{digest}").mock(
        return_value=httpx.Response(200, json={})
    )
    registry = GhcrRegistry()
    assert registry.probe_ownership(_REPOSITORY, "ocx.sh/kitware/cmake") == "unconfirmed"


@respx.mock
def test_probe_ownership_confirmed() -> None:
    digest = "sha256:" + "e" * 64
    respx.head(_DESC_URL).mock(
        return_value=httpx.Response(200, headers={"Docker-Content-Digest": digest})
    )
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/{digest}").mock(
        return_value=httpx.Response(
            200, json={"annotations": {"sh.ocx.name": "ocx.sh/kitware/cmake"}}
        )
    )
    registry = GhcrRegistry()
    assert registry.probe_ownership(_REPOSITORY, "ocx.sh/kitware/cmake") == "confirmed"


@respx.mock
def test_probe_ownership_mismatch() -> None:
    digest = "sha256:" + "f" * 64
    respx.head(_DESC_URL).mock(
        return_value=httpx.Response(200, headers={"Docker-Content-Digest": digest})
    )
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/{digest}").mock(
        return_value=httpx.Response(
            200, json={"annotations": {"sh.ocx.name": "ocx.sh/someone-else/cmake"}}
        )
    )
    registry = GhcrRegistry()
    assert registry.probe_ownership(_REPOSITORY, "ocx.sh/kitware/cmake") == "mismatch"
