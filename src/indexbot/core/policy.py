"""`.github/index-policy.json` parsing — this deployment's own policy input.

OCX's index is "one format, many copies": the public `ocx-sh/index` and any
corporate copy run the same bot over the same wire format but do not share a
registry-host policy — a corporate operator's physical bytes live on their
Harbor/Artifactory/ECR, not on `ghcr.io`. G-03's allowlist is therefore a
*deployment* input, read from a file each index repo commits for itself, not a
constant compiled into `core/validate_entry.py`.

**Why a committed file and not an environment/Actions variable.** G-03's whole
control is "extend only via reviewed PR" — the allowlist is the set of hosts
every ocx client will follow to fetch bytes, so widening it is a supply-chain
trust decision. An env var or `vars.`/`secrets.` entry can be changed by
anyone with repo-settings access, silently, with no diff and no reviewer.
A committed file keeps the property mechanical: widening trust *is* a PR, and
`.github/**` is exactly the surface branch protection and CODEOWNERS already
guard. `.github/` is also where this repo already keeps its other bot-read,
PR-reviewed governance data (`maintainers.yml`, G-20).

**Why not a JSON Schema of its own.** `schema/*.schema.json` is the *served*
wire contract (`$id: https://index.ocx.sh/schema/...`), sealed by
`adr_locked_observation_index_format.md` D7; this file is a bot-side
deployment artifact that is never served and never part of the index format.
Its whole grammar is one required key holding a non-empty array of bare hosts
— `parse_index_policy` below is that grammar's single source of truth, it runs
in CI on every `indexbot` invocation that needs a policy, and
`tests/security/test_governance_contracts.py` parses the committed file itself.
A second copy of five rules in `schema/` would only be able to drift.

JSON (stdlib `json`), not YAML: unlike `core/maintainers.py`'s and
`cli/seed_import.py`'s hand-rolled `key: value` readers, this needs no parser
of its own at all, and `bot/pyproject.toml` still declares no YAML dependency
(`httpx` is the one runtime dep, ADR-4 BD-1's minimal-footprint driver).
"""

from __future__ import annotations

import json
import re
from typing import Any, Final, cast

from indexbot.errors import ValidationError

INDEX_POLICY_PATH: Final[str] = ".github/index-policy.json"
"""Repo-relative path of the policy file, read through a `FilePort` (local
checkout) or a `GitHubPort` at `main` (`cli/announce.py`'s publisher lane runs
outside any index checkout — see `cli/_wiring.py`)."""

_HOSTS_KEY: Final[str] = "registry_hosts"

_HOST_MAX_LENGTH: Final[int] = 253  # RFC 1035 total domain-name length
_HOST_LABEL = r"[a-z0-9]+(?:-+[a-z0-9]+)*"
_HOST_RE: Final[re.Pattern[str]] = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*")
"""A bare, lowercase registry host: DNS labels only, no scheme, no port, no
path, no trailing dot.

Deliberately strict, because every rejected shape here would otherwise be a
silent never-match: `check_repository_allowlisted` compares against
`urlsplit(repository).hostname`, which is always lowercased and never carries
a port — so a policy entry of `https://harbor.corp`, `Harbor.Corp` or
`harbor.corp:5000` would parse fine, allowlist nothing, and read as "the bot
ignores my policy". A registry served on a non-default port is still
allowlisted by its bare host (`harbor.corp` admits
`oci://harbor.corp:5000/team/tool`). A single label (`harbor`) is legal —
internal registries often have no dot.
"""


def parse_index_policy(raw: bytes) -> frozenset[str]:
    """Parse `.github/index-policy.json` bytes into the allowed registry hosts.

    Raises `ValidationError` on anything but exactly
    `{"registry_hosts": ["<host>", ...]}` with at least one entry: malformed
    JSON, a non-object document, an unknown key (a typo'd `registry_host`
    would otherwise silently leave the deployment with no policy at all), a
    missing/non-array `registry_hosts`, an empty array, or an entry that is
    not a bare lowercase host per `_HOST_RE`.

    Duplicates are not an error — the result is a `frozenset`.
    """
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{INDEX_POLICY_PATH}: malformed JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"{INDEX_POLICY_PATH}: must be a JSON object")
    data = cast("dict[str, Any]", parsed)

    unknown = sorted(set(data) - {_HOSTS_KEY})
    if unknown:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: unknown key(s) {unknown} — the only supported key is "
            f"{_HOSTS_KEY!r}"
        )
    if _HOSTS_KEY not in data:
        raise ValidationError(f"{INDEX_POLICY_PATH}: missing required {_HOSTS_KEY!r} key")

    hosts_raw: Any = data[_HOSTS_KEY]
    if not isinstance(hosts_raw, list):
        raise ValidationError(f"{INDEX_POLICY_PATH}: {_HOSTS_KEY!r} must be an array of hosts")
    hosts = cast("list[Any]", hosts_raw)
    if not hosts:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: {_HOSTS_KEY!r} must list at least one host — an empty "
            "allowlist rejects every package root this index could ever serve"
        )
    for host in hosts:
        if not isinstance(host, str) or len(host) > _HOST_MAX_LENGTH:
            raise ValidationError(f"{INDEX_POLICY_PATH}: {host!r} is not a registry host")
        if _HOST_RE.fullmatch(host) is None:
            raise ValidationError(
                f"{INDEX_POLICY_PATH}: {host!r} is not a bare lowercase registry host "
                "(no scheme, no port, no path)"
            )
    return frozenset(cast("list[str]", hosts))
