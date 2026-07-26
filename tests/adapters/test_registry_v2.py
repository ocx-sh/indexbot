"""Tests for `adapters/registry_v2.py` — CONTRACTS.md §9.

One `respx.mock`-decorated test per distinct response class
(200/404/401-then-retry/429-with-and-without-Retry-After/5xx-exhausted/
malformed-JSON/pagination/page-bound-exceeded), per CONTRACTS.md §2's test
conventions. The 401/429/5xx retry machinery lives in `RegistryV2._send`,
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

from indexbot.adapters import registry_v2
from indexbot.adapters.registry_v2 import RegistryV2, RoutedRegistry
from indexbot.errors import AnomalyError, TransientError, ValidationError
from indexbot.ports import RegistryPort

_BASE = "https://ghcr.io"
_REPO_PATH = "ocx-contrib/cmake"
_REPOSITORY = f"oci://ghcr.io/{_REPO_PATH}"
_TAGS_URL = f"{_BASE}/v2/{_REPO_PATH}/tags/list"
_DESC_URL = f"{_BASE}/v2/{_REPO_PATH}/manifests/__ocx.desc"


def _no_sleep(seconds: float) -> None:
    del seconds  # test double — skip the real backoff wait


def test_ghcr_registry_conforms_to_registry_port() -> None:
    registry: RegistryPort = RegistryV2()
    assert isinstance(registry, RegistryV2)


# --- list_tags ---------------------------------------------------------


@respx.mock
def test_list_tags_single_page() -> None:
    respx.get(_TAGS_URL, params={"n": "100"}).mock(
        return_value=httpx.Response(200, json={"tags": ["latest", "1.0.0"]})
    )
    registry = RegistryV2()
    assert registry.list_tags(_REPOSITORY) == ["latest", "1.0.0"]


@respx.mock
def test_list_tags_404_returns_empty_list() -> None:
    respx.get(_TAGS_URL, params={"n": "100"}).mock(return_value=httpx.Response(404))
    registry = RegistryV2()
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
    registry = RegistryV2()
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
    registry = RegistryV2(max_pages=2)
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
    registry = RegistryV2()
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
    registry = RegistryV2()
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
    registry = RegistryV2()
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
    registry = RegistryV2()
    assert registry.list_tags(_REPOSITORY) == ["1.0.0"]


# --- get_manifest (also covers the shared 401/429/5xx retry machinery) --


@respx.mock
def test_get_manifest_returns_parsed_json() -> None:
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(
            200, json={"mediaType": "application/vnd.oci.image.manifest.v1+json"}
        )
    )
    registry = RegistryV2()
    fetch = registry.get_manifest(_REPOSITORY, "v1.0.0")
    assert fetch.parsed["mediaType"] == "application/vnd.oci.image.manifest.v1+json"


@respx.mock
def test_get_manifest_digest_is_computed_from_response_body() -> None:
    body = b'{"mediaType": "application/vnd.oci.image.manifest.v1+json"}'
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(200, content=body)
    )
    registry = RegistryV2()
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
    registry = RegistryV2()
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
    registry = RegistryV2()
    with pytest.raises(AnomalyError, match="digest mismatch"):
        registry.get_manifest(_REPOSITORY, "v1.0.0")


@respx.mock
def test_get_manifest_404_raises_keyerror() -> None:
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/missing").mock(return_value=httpx.Response(404))
    registry = RegistryV2()
    with pytest.raises(KeyError):
        registry.get_manifest(_REPOSITORY, "missing")


@respx.mock
def test_get_manifest_malformed_json_raises_value_error() -> None:
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(200, content=b"not-json{")
    )
    registry = RegistryV2()
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
    registry = RegistryV2()
    # The retried request only succeeds because a token was fetched and
    # attached — the second manifest response is only reached via that path.
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    assert token_route.call_count == 1


@respx.mock
def test_get_manifest_persistent_401_raises_transient() -> None:
    respx.get(f"{_BASE}/token").mock(return_value=httpx.Response(200, json={"token": "tok-1"}))
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(return_value=httpx.Response(401))
    registry = RegistryV2()
    with pytest.raises(TransientError, match="persistent 401"):
        registry.get_manifest(_REPOSITORY, "v1.0.0")


# --- 403 DENIED (missing/private repository) ----------------------------


@respx.mock
def test_token_endpoint_403_with_denied_body_raises_validation_error() -> None:
    respx.get(f"{_BASE}/token").mock(
        return_value=httpx.Response(
            403, json={"errors": [{"code": "DENIED", "message": "requested access is denied"}]}
        )
    )
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(return_value=httpx.Response(401))
    registry = RegistryV2()
    with pytest.raises(ValidationError, match=_REPO_PATH):
        registry.get_manifest(_REPOSITORY, "v1.0.0")


@respx.mock
def test_token_endpoint_403_with_empty_body_raises_validation_error() -> None:
    respx.get(f"{_BASE}/token").mock(return_value=httpx.Response(403))
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(return_value=httpx.Response(401))
    registry = RegistryV2()
    with pytest.raises(ValidationError, match=_REPO_PATH):
        registry.get_manifest(_REPOSITORY, "v1.0.0")


@respx.mock
def test_v2_manifest_403_raises_validation_error_not_transient() -> None:
    # No 401 first — the resource server itself denies the anonymous request
    # outright, no token retry possible.
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(return_value=httpx.Response(403))
    registry = RegistryV2()
    with pytest.raises(ValidationError, match=_REPO_PATH):
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
    registry = RegistryV2()
    registry.get_manifest(_REPOSITORY, "v1.0.0")
    registry.get_manifest(_REPOSITORY, "v1.0.0")
    assert token_route.call_count == 1


@respx.mock
def test_429_without_retry_after_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_v2.time, "sleep", _no_sleep)
    route = respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": True})]
    )
    registry = RegistryV2()
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    assert route.call_count == 2


@respx.mock
def test_429_with_retry_after_uses_server_value(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(registry_v2.time, "sleep", sleeps.append)
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    registry = RegistryV2()
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    # Retry-After wins outright over the exponential/jitter formula (G-10).
    assert sleeps == [2.0]


@respx.mock
def test_429_with_unparseable_retry_after_falls_back_to_exponential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(registry_v2.time, "sleep", sleeps.append)
    monkeypatch.setattr(registry_v2.random, "random", lambda: 0.5)
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "not-a-number"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    registry = RegistryV2()
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    # base_delay_seconds(1.0) * 2**0 * (0.5 + jitter(0.5)) == 1.0
    assert sleeps == [1.0]


@respx.mock
def test_429_with_non_positive_retry_after_falls_back_to_exponential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(registry_v2.time, "sleep", sleeps.append)
    monkeypatch.setattr(registry_v2.random, "random", lambda: 0.5)
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    registry = RegistryV2()
    assert registry.get_manifest(_REPOSITORY, "v1.0.0").parsed == {"ok": True}
    assert sleeps == [1.0]


@respx.mock
def test_5xx_exhausted_raises_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_v2.time, "sleep", _no_sleep)
    route = respx.get(f"{_BASE}/v2/{_REPO_PATH}/manifests/v1.0.0").mock(
        return_value=httpx.Response(503)
    )
    registry = RegistryV2()
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
    registry = RegistryV2()
    assert registry.get_desc_tag_digest(_REPOSITORY) == digest


@respx.mock
def test_get_desc_tag_digest_absent_returns_none() -> None:
    respx.head(_DESC_URL).mock(return_value=httpx.Response(404))
    registry = RegistryV2()
    assert registry.get_desc_tag_digest(_REPOSITORY) is None


# --- get_blob ------------------------------------------------------------


@respx.mock
def test_get_blob_returns_bytes() -> None:
    digest = "sha256:" + "b" * 64
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/blobs/{digest}").mock(
        return_value=httpx.Response(200, content=b"# Readme")
    )
    registry = RegistryV2()
    assert registry.get_blob(_REPOSITORY, digest) == b"# Readme"


@respx.mock
def test_get_blob_missing_raises_keyerror() -> None:
    digest = "sha256:" + "c" * 64
    respx.get(f"{_BASE}/v2/{_REPO_PATH}/blobs/{digest}").mock(return_value=httpx.Response(404))
    registry = RegistryV2()
    with pytest.raises(KeyError):
        registry.get_blob(_REPOSITORY, digest)


# --- probe_ownership -------------------------------------------------------


@respx.mock
def test_probe_ownership_unconfirmed_when_no_desc_tag() -> None:
    respx.head(_DESC_URL).mock(return_value=httpx.Response(404))
    registry = RegistryV2()
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
    registry = RegistryV2()
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
    registry = RegistryV2()
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
    registry = RegistryV2()
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
    registry = RegistryV2()
    assert registry.probe_ownership(_REPOSITORY, "ocx.sh/kitware/cmake") == "mismatch"


# --- the ocx.sh host (same client, different token endpoint) -----------


_OCX_SH_REPO_PATH = "ocx/cli"
_OCX_SH_REPOSITORY = f"oci://ocx.sh/{_OCX_SH_REPO_PATH}"


def _ocx_sh_registry() -> RegistryV2:
    """Exactly what `cli/_wiring._registry()` wires for `ocx.sh`."""
    return RegistryV2(
        base_url=f"https://{registry_v2.OCX_SH_HOST}",
        host=registry_v2.OCX_SH_HOST,
        realm=registry_v2.OCX_SH_REALM,
    )


def test_token_url_defaults_to_base_url_token() -> None:
    """The Registry v2 convention GHCR follows — and the seam the integration
    harness leans on: a fake at `http://127.0.0.1:<port>` serves its own
    `/token`, so the endpoint has to move with `base_url`."""
    assert RegistryV2().realm == "https://ghcr.io/token"
    assert RegistryV2(base_url="http://127.0.0.1:9").realm == "http://127.0.0.1:9/token"


@respx.mock
def test_ocx_sh_401_takes_the_token_from_the_artifactory_endpoint() -> None:
    """`ocx.sh` is anonymously readable but only via its own realm: a client
    that fetched `https://ocx.sh/token` (GHCR's shape) would get a 404 HTML
    page and never authenticate. The 401 dance is otherwise identical."""
    tags_url = f"https://ocx.sh/v2/{_OCX_SH_REPO_PATH}/tags/list"
    token_route = respx.get(registry_v2.OCX_SH_REALM).mock(
        return_value=httpx.Response(200, json={"token": "anon-pull-token"})
    )
    respx.get(tags_url, params={"n": "100"}).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"tags": ["0.4.0", "latest"]}),
        ]
    )

    assert _ocx_sh_registry().list_tags(_OCX_SH_REPOSITORY) == ["0.4.0", "latest"]

    assert token_route.call_count == 1
    token_request_url = httpx.URL(str(token_route.calls.last.request.url))
    assert token_request_url.params["service"] == "ocx.sh"
    assert token_request_url.params["scope"] == f"repository:{_OCX_SH_REPO_PATH}:pull"


@respx.mock
def test_ocx_sh_missing_repository_lists_no_tags() -> None:
    """404 from `tags/list` is "nothing observed here", not an error — same
    contract as GHCR's, asserted on the host whose 404 body differs
    (`NAME_UNKNOWN` from Artifactory)."""
    respx.get(f"https://ocx.sh/v2/{_OCX_SH_REPO_PATH}/tags/list", params={"n": "100"}).mock(
        return_value=httpx.Response(404, json={"errors": [{"code": "NAME_UNKNOWN"}]})
    )
    assert _ocx_sh_registry().list_tags(_OCX_SH_REPOSITORY) == []


@respx.mock
def test_ocx_sh_missing_manifest_raises_keyerror() -> None:
    respx.get(f"https://ocx.sh/v2/{_OCX_SH_REPO_PATH}/manifests/9.9.9").mock(
        return_value=httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})
    )
    with pytest.raises(KeyError):
        _ocx_sh_registry().get_manifest(_OCX_SH_REPOSITORY, "9.9.9")


@respx.mock
def test_ocx_sh_403_names_its_own_host_not_ghcr() -> None:
    """The permanent-denial message must name the registry that denied it —
    a `ValidationError` saying `ghcr.io/ocx/cli` would send the reader to the
    wrong registry entirely."""
    respx.get(registry_v2.OCX_SH_REALM).mock(return_value=httpx.Response(403))
    respx.get(f"https://ocx.sh/v2/{_OCX_SH_REPO_PATH}/tags/list", params={"n": "100"}).mock(
        return_value=httpx.Response(401)
    )
    with pytest.raises(ValidationError, match=r"ocx\.sh/ocx/cli is missing"):
        _ocx_sh_registry().list_tags(_OCX_SH_REPOSITORY)


# --- RoutedRegistry (per-call host dispatch) ---------------------------


def _router() -> RoutedRegistry:
    return RoutedRegistry({"ghcr.io": RegistryV2(), "ocx.sh": _ocx_sh_registry()})


def test_routed_registry_conforms_to_registry_port() -> None:
    registry: RegistryPort = _router()
    assert isinstance(registry, RoutedRegistry)


@respx.mock
def test_routed_registry_sends_each_host_to_its_own_client() -> None:
    """The property the whole two-host wiring rests on: one `validate` run
    can carry roots from both registries, and each must reach its own
    endpoint — never the other's."""
    ghcr_route = respx.get(_TAGS_URL, params={"n": "100"}).mock(
        return_value=httpx.Response(200, json={"tags": ["1.0.0"]})
    )
    ocx_route = respx.get(
        f"https://ocx.sh/v2/{_OCX_SH_REPO_PATH}/tags/list", params={"n": "100"}
    ).mock(return_value=httpx.Response(200, json={"tags": ["0.4.0"]}))

    router = _router()
    assert router.list_tags(_REPOSITORY) == ["1.0.0"]
    assert router.list_tags(_OCX_SH_REPOSITORY) == ["0.4.0"]

    assert ghcr_route.call_count == 1
    assert ocx_route.call_count == 1


@respx.mock
def test_routed_registry_delegates_every_port_method() -> None:
    """Every `RegistryPort` method routes, not just `list_tags` — a method
    that forgot to would silently read the wrong registry."""
    body = b'{"annotations": {"sh.ocx.name": "ocx.sh/ocx/cli"}}'
    digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
    base = f"https://ocx.sh/v2/{_OCX_SH_REPO_PATH}"
    respx.head(f"{base}/manifests/__ocx.desc").mock(
        return_value=httpx.Response(200, headers={"Docker-Content-Digest": digest})
    )
    respx.get(f"{base}/manifests/{digest}").mock(return_value=httpx.Response(200, content=body))
    respx.get(f"{base}/blobs/{digest}").mock(return_value=httpx.Response(200, content=b"blob"))

    router = _router()
    assert router.get_desc_tag_digest(_OCX_SH_REPOSITORY) == digest
    assert router.get_manifest(_OCX_SH_REPOSITORY, digest).raw == body
    assert router.get_blob(_OCX_SH_REPOSITORY, digest) == b"blob"
    assert router.probe_ownership(_OCX_SH_REPOSITORY, "ocx.sh/ocx/cli") == "confirmed"


def test_routed_registry_refuses_a_host_it_has_no_client_for() -> None:
    """Defence in depth behind G-03: the allowlist and `_registry_hosts`
    already refuse an unservable host long before this point, so reaching
    here means a wiring bug — and it must fail closed (no request attempted),
    exactly like an out-of-policy host does."""
    with pytest.raises(ValidationError, match="no registry client for host"):
        _router().list_tags("oci://harbor.corp.internal/team/thing")
