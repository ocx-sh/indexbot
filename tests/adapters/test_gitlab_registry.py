"""`registry.gitlab.com` — a third registry host on the same Registry v2 client.

GHCR and `ocx.sh` both happen to name themselves in the token request's
`service` parameter, so the adapter passed `host` for it and nobody noticed
the coupling. GitLab does not: it advertises

    WWW-Authenticate: Bearer realm="https://gitlab.com/jwt/auth",
                             service="container_registry"

— a fixed literal that is not the host. Probed against the live registry on
2026-08-24. Sending `service=registry.gitlab.com` there returns a token that
is not valid for the scope, so this is the difference between an index whose
downloads work and one whose downloads 401.

Every GitLab-hosted index depends on this: a project's built-in container
registry is where a GitLab-native publisher's bytes naturally live, and it is
a host no OCX deployment has ever allowlisted.
"""

from __future__ import annotations

import httpx
import pytest

from ocx_indexbot.adapters.registry_v2 import (
    GITLAB_HOST,
    GITLAB_REALM,
    RegistryV2,
)
from ocx_indexbot.cli import _wiring


def test_gitlab_sends_the_literal_container_registry_service() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # The real dance: an unauthenticated /v2/ call 401s, the client
        # fetches a pull token, and retries with it attached.
        if request.url.path == "/jwt/auth":
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"token": "t"})
        if "authorization" not in request.headers:
            return httpx.Response(401)
        return httpx.Response(200, json={"tags": ["1.0.0"]})

    registry = RegistryV2(
        base_url="https://registry.gitlab.com",
        host=GITLAB_HOST,
        realm=GITLAB_REALM,
        service="container_registry",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    registry.list_tags("group/project")

    assert seen["service"] == "container_registry", (
        "GitLab issues a token for the service name, not the host it is served from"
    )
    assert seen["scope"] == "repository:group/project:pull"


def test_service_defaults_to_the_host_for_every_other_registry() -> None:
    """GHCR and `ocx.sh` keep the behaviour they always had — the new field
    is opt-in, so no existing deployment's token request changes shape."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"token": "t"})
        if "authorization" not in request.headers:
            return httpx.Response(401)
        return httpx.Response(200, json={"tags": []})

    registry = RegistryV2(client=httpx.Client(transport=httpx.MockTransport(handler)))
    registry.list_tags("ocx-contrib/cmake")

    assert seen["service"] == "ghcr.io"


def test_gitlab_is_a_servable_host() -> None:
    """A GitLab-hosted index puts its bytes on `registry.gitlab.com`, and the
    two facts no convention supplies — the `jwt/auth` realm and the fixed
    `container_registry` service — come from the built-in table, so a bare
    entry is enough."""
    policy = _wiring._index_policy(  # pyright: ignore[reportPrivateUsage]
        b'{"name": "e2e.ocx.sh", "name_segments": 2, "registry_hosts": ["registry.gitlab.com"]}'
    )
    client = _wiring._registry(policy, credentialed=True).by_host[GITLAB_HOST]  # pyright: ignore[reportPrivateUsage]
    assert client.realm == GITLAB_REALM
    assert client.service == "container_registry"


def test_gitlab_policy_is_accepted_at_wiring_time() -> None:
    policy = (
        b'{"name": "e2e.ocx.sh", "name_segments": 2, "registry_hosts": ["registry.gitlab.com"]}'
    )
    parsed = _wiring._index_policy(policy)  # pyright: ignore[reportPrivateUsage]
    assert parsed.registry_hosts == frozenset({GITLAB_HOST})


def test_the_realm_is_pinned_not_followed_from_the_response() -> None:
    """Same SSRF argument as `OCX_SH_REALM`: a `realm` is a server-supplied
    URL, and this adapter never follows one it was handed."""
    assert GITLAB_REALM == "https://gitlab.com/jwt/auth"


@pytest.mark.parametrize("host", ["ghcr.io", "ocx.sh", GITLAB_HOST, "harbor.corp.internal"])
def test_every_allowlisted_host_is_dispatchable(host: str) -> None:
    """The invariant that replaced the compiled-in host table: whatever a
    policy allowlists gets a client, built from that entry — including a host
    this package has never heard of."""
    policy = _wiring._index_policy(  # pyright: ignore[reportPrivateUsage]
        b'{"name": "acme.corp", "name_segments": 2, "registry_hosts": ["' + host.encode() + b'"]}'
    )
    assert set(_wiring._registry(policy, credentialed=True).by_host) == {host}  # pyright: ignore[reportPrivateUsage]
