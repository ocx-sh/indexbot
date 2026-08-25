"""`core/policy.py` — `.github/index-policy.json` parsing.

Every rejection below is a shape that would otherwise be a *silent* policy
failure: a typo'd key, a scheme-prefixed or upper-cased host, a host carrying
a port. `check_repository_allowlisted` compares against
`urlsplit().hostname`, so each of those would parse fine and then match
nothing — "the bot ignores my policy" is the bug class this parser exists to
turn into an error message.

v2 (0.2.0) widened the file from a registry allowlist into the deployment's
whole identity: the logical name prefix every root carries, how many segments
a package id has under this index, which namespaces the operator reserves,
and which forge/merge policy the governance lane runs. `name` and
`name_segments` are **required with no defaults** — a default of `ocx.sh` is
precisely the hardcode 0.2.0 exists to remove, and a silent default would
reintroduce it for every index that forgot to declare one.
"""

from __future__ import annotations

import pytest

from ocx_indexbot.core.policy import (
    DEFAULT_RECONCILE_CRON,
    DEFAULT_STALE_CRON,
    MAX_NAME_SEGMENTS,
    CiConfig,
    IndexPolicy,
    RegistryConfig,
    parse_index_policy,
    registry_credential_env,
    resolves_at_runtime,
)
from ocx_indexbot.errors import ValidationError

_MINIMAL = b"""
{"name": "ocx.sh", "name_segments": 2, "registry_hosts": ["ghcr.io"]}
"""


def _policy(**overrides: object) -> bytes:
    import json

    doc: dict[str, object] = {
        "name": "ocx.sh",
        "name_segments": 2,
        "registry_hosts": ["ghcr.io"],
    }
    doc.update(overrides)
    return json.dumps(doc).encode()


# --- the whole document -----------------------------------------------------


def test_parses_the_minimal_shape() -> None:
    policy = parse_index_policy(_MINIMAL)
    assert policy == IndexPolicy(
        name="ocx.sh",
        name_segments=2,
        registries={"ghcr.io": RegistryConfig(host="ghcr.io")},
        reserved_namespaces=frozenset(),
        auto_merge="owners",
        ci=CiConfig(
            forge="github",
            owner="",
            run="",
            setup="",
            deploy_workflow="",
            reconcile_cron=DEFAULT_RECONCILE_CRON,
            stale_cron=DEFAULT_STALE_CRON,
        ),
    )


def test_parses_the_full_shape() -> None:
    raw = _policy(
        name="acme.corp",
        name_segments=3,
        registry_hosts=["harbor.corp", "ghcr.io"],
        reserved_namespaces=["acme", "admin"],
        governance={"auto_merge": "never"},
        ci={"forge": "gitlab"},
    )
    policy = parse_index_policy(raw)
    assert policy.name == "acme.corp"
    assert policy.name_segments == 3
    assert policy.registry_hosts == frozenset({"harbor.corp", "ghcr.io"})
    assert policy.reserved_namespaces == frozenset({"acme", "admin"})
    assert policy.auto_merge == "never"
    assert policy.ci.forge == "gitlab"


def test_schema_key_is_accepted_and_ignored() -> None:
    """The committed file carries `$schema` so an editor can autocomplete it.
    The parser must not treat it as an unknown key — and must not care what it
    points at, since nothing validates against the schema at runtime."""
    raw = _policy(**{"$schema": "https://ocx-sh.github.io/indexbot/schema/x.json"})
    assert parse_index_policy(raw).name == "ocx.sh"


def test_malformed_json_raises() -> None:
    with pytest.raises(ValidationError, match="malformed JSON"):
        parse_index_policy(b"{not json")


def test_non_object_document_raises() -> None:
    with pytest.raises(ValidationError, match="must be a JSON object"):
        parse_index_policy(b'["ghcr.io"]')


def test_unknown_key_raises() -> None:
    """A typo'd key would otherwise leave the deployment with no policy while
    looking like it had one."""
    with pytest.raises(ValidationError, match="unknown key"):
        parse_index_policy(_policy(registry_host=["ghcr.io"]))


# --- name (the logical prefix) ----------------------------------------------


def test_missing_name_raises() -> None:
    with pytest.raises(ValidationError, match="missing required 'name'"):
        parse_index_policy(b'{"name_segments": 2, "registry_hosts": ["ghcr.io"]}')


@pytest.mark.parametrize(
    "name",
    [
        "https://ocx.sh",  # scheme
        "OCX.SH",  # uppercase
        "ocx.sh:443",  # port
        "ocx.sh/p",  # path
        "ocx.sh.",  # trailing dot
        "-ocx.sh",  # leading hyphen
        "",  # empty
    ],
)
def test_non_bare_name_raises(name: str) -> None:
    """`name` is the registry-namespace key an ocx client configures
    (`[registries."<name>"] index = …`) and the literal prefix of every
    `root.name`. It has exactly the host grammar, for the same reason
    `registry_hosts` does."""
    with pytest.raises(ValidationError, match="'name'"):
        parse_index_policy(_policy(name=name))


def test_non_string_name_raises() -> None:
    with pytest.raises(ValidationError, match="'name'"):
        parse_index_policy(_policy(name=42))


# --- name_segments ----------------------------------------------------------


def test_missing_name_segments_raises() -> None:
    with pytest.raises(ValidationError, match="missing required 'name_segments'"):
        parse_index_policy(b'{"name": "ocx.sh", "registry_hosts": ["ghcr.io"]}')


def test_single_segment_is_legal() -> None:
    """A flat index (`ocx.sh/<pkg>`) is a legal shape — the ocx client reads
    `name_segments` from `config.json` precisely so it can resolve one."""
    assert parse_index_policy(_policy(name_segments=1)).name_segments == 1


def test_max_segments_is_legal() -> None:
    raw = _policy(name_segments=MAX_NAME_SEGMENTS)
    assert parse_index_policy(raw).name_segments == MAX_NAME_SEGMENTS


@pytest.mark.parametrize("segments", [0, -1, MAX_NAME_SEGMENTS + 1])
def test_out_of_range_name_segments_raises(segments: int) -> None:
    with pytest.raises(ValidationError, match="'name_segments'"):
        parse_index_policy(_policy(name_segments=segments))


@pytest.mark.parametrize("segments", ["2", 2.0, None])
def test_non_integer_name_segments_raises(segments: object) -> None:
    with pytest.raises(ValidationError, match="'name_segments'"):
        parse_index_policy(_policy(name_segments=segments))


def test_boolean_name_segments_raises() -> None:
    """`isinstance(True, int)` is True in Python, so a bare int check would
    accept `true` and silently mean 1."""
    with pytest.raises(ValidationError, match="'name_segments'"):
        parse_index_policy(_policy(name_segments=True))


# --- registry_hosts (v1 behaviour, unchanged) -------------------------------


def test_parses_multiple_hosts_and_dedups() -> None:
    raw = _policy(registry_hosts=["ghcr.io", "harbor.corp.internal", "ghcr.io"])
    assert parse_index_policy(raw).registry_hosts == frozenset({"ghcr.io", "harbor.corp.internal"})


def test_accepts_a_single_label_host() -> None:
    """Internal registries often have no dot — `harbor` is a legal host."""
    assert parse_index_policy(_policy(registry_hosts=["harbor"])).registry_hosts == frozenset(
        {"harbor"}
    )


def test_missing_registry_hosts_raises() -> None:
    with pytest.raises(ValidationError, match="missing required 'registry_hosts'"):
        parse_index_policy(b'{"name": "ocx.sh", "name_segments": 2}')


def test_non_array_registry_hosts_raises() -> None:
    with pytest.raises(ValidationError, match="must be an array"):
        parse_index_policy(_policy(registry_hosts="ghcr.io"))


def test_empty_registry_hosts_raises() -> None:
    with pytest.raises(ValidationError, match="at least one host"):
        parse_index_policy(_policy(registry_hosts=[]))


def test_non_string_host_entry_raises() -> None:
    with pytest.raises(ValidationError, match="is not a registry host"):
        parse_index_policy(_policy(registry_hosts=[42]))


def test_over_long_host_entry_raises() -> None:
    with pytest.raises(ValidationError, match="is not a registry host"):
        parse_index_policy(_policy(registry_hosts=["a" * 254]))


@pytest.mark.parametrize(
    "host",
    [
        "https://ghcr.io",  # scheme
        "GHCR.IO",  # uppercase — hostname comparison is always lowercase
        "ghcr.io:5000",  # port — never part of urlsplit().hostname
        "ghcr.io/ocx-contrib",  # path
        "ghcr.io.",  # trailing dot
        "-ghcr.io",  # leading hyphen
        "",  # empty
    ],
)
def test_non_bare_host_raises(host: str) -> None:
    with pytest.raises(ValidationError, match="bare lowercase registry host"):
        parse_index_policy(_policy(registry_hosts=[host]))


# --- registry_hosts: object entries -----------------------------------------


def _entry(**fields: object) -> bytes:
    return _policy(registry_hosts=[{"host": "artifactory.corp", **fields}])


def test_object_entry_defaults_to_an_anonymous_token_registry() -> None:
    """The only required key is `host`. Everything else has a convention
    behind it, and an entry that states nothing more describes exactly what
    a bare string would."""
    config = parse_index_policy(_entry()).registries["artifactory.corp"]
    assert config == RegistryConfig(host="artifactory.corp")
    assert config.auth == "token"
    assert config.credentials_env == ""


def test_object_entry_carries_address_realm_service_auth_and_credential() -> None:
    config = parse_index_policy(
        _entry(
            base_url="https://oci.artifactory.corp:8443",
            realm="https://oci.artifactory.corp:8443/v2/token",
            service="artifactory",
            auth="basic",
            credentials_env="OCX_REGISTRY_ART",
        )
    ).registries["artifactory.corp"]
    assert config.base_url == "https://oci.artifactory.corp:8443"
    assert config.realm == "https://oci.artifactory.corp:8443/v2/token"
    assert config.service == "artifactory"
    assert config.auth == "basic"
    assert config.credentials_env == "OCX_REGISTRY_ART"


def test_registry_hosts_mixes_bare_strings_and_objects() -> None:
    raw = _policy(
        registry_hosts=["ghcr.io", {"host": "artifactory.corp", "credentials_env": "ART"}]
    )
    policy = parse_index_policy(raw)
    assert policy.registry_hosts == frozenset({"ghcr.io", "artifactory.corp"})
    assert policy.registries["ghcr.io"].credentials_env == ""


def test_object_entry_missing_host_raises() -> None:
    with pytest.raises(ValidationError, match="missing required 'host'"):
        parse_index_policy(_policy(registry_hosts=[{"base_url": "https://oci.corp"}]))


def test_object_entry_with_a_non_bare_host_raises() -> None:
    with pytest.raises(ValidationError, match="bare lowercase registry host"):
        parse_index_policy(_policy(registry_hosts=[{"host": "oci.corp:8443"}]))


def test_object_entry_unknown_key_raises() -> None:
    with pytest.raises(ValidationError, match="unknown key"):
        parse_index_policy(_entry(token="hunter2"))  # noqa: S106


@pytest.mark.parametrize(
    ("base_url", "match"),
    [
        ("ftp://oci.corp", "must start with"),
        ("https://", "must include a host"),
        ("https://svc:secret@oci.corp", "must not embed credentials"),
        ("https://oci.corp?x=1", "no query or fragment"),
        ("https://oci.corp#frag", "no query or fragment"),
        ("https://oci.corp/", "must not end with"),
    ],
)
def test_bad_base_url_raises(base_url: str, match: str) -> None:
    """`base_url` is used verbatim as a request-URL prefix, so it is validated
    as one — and a credential inside it would be a secret in a committed,
    reviewed file."""
    with pytest.raises(ValidationError, match=match):
        parse_index_policy(_entry(base_url=base_url))


def test_non_string_base_url_raises() -> None:
    with pytest.raises(ValidationError, match="'base_url' must be a string URL"):
        parse_index_policy(_entry(base_url=8443))


def test_over_long_base_url_raises() -> None:
    with pytest.raises(ValidationError, match="'base_url' must be a string URL"):
        parse_index_policy(_entry(base_url="https://oci.corp/" + "a" * 2048))


@pytest.mark.parametrize(
    ("realm", "match"),
    [
        ("oci.corp/token", r"must be an http\(s\) URL"),
        ("https://svc:secret@oci.corp/token", "must not embed credentials"),
        (7, "must be a string URL"),
    ],
)
def test_bad_realm_raises(realm: object, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        parse_index_policy(_entry(realm=realm))


def test_non_string_service_raises() -> None:
    with pytest.raises(ValidationError, match="'service' must be a single-line string"):
        parse_index_policy(_entry(service="container\nregistry"))


def test_unknown_auth_mode_raises() -> None:
    with pytest.raises(ValidationError, match="'auth' must be one of"):
        parse_index_policy(_entry(auth="mtls"))


@pytest.mark.parametrize("name", ["1_LEADING_DIGIT", "HAS-HYPHEN", "has space", "$INTERPOLATED"])
def test_bad_credentials_env_name_raises(name: str) -> None:
    """The field names an environment variable; anything that cannot be one
    would silently mean "anonymous" at wiring time."""
    with pytest.raises(ValidationError, match="not a bare environment-variable name"):
        parse_index_policy(_entry(credentials_env=name))


def test_non_string_credentials_env_raises() -> None:
    with pytest.raises(ValidationError, match="must be an environment-variable name"):
        parse_index_policy(_entry(credentials_env=["ART"]))


def test_duplicate_host_with_identical_configuration_is_accepted() -> None:
    raw = _policy(registry_hosts=["ghcr.io", "ghcr.io"])
    assert parse_index_policy(raw).registry_hosts == frozenset({"ghcr.io"})


def test_duplicate_host_with_conflicting_configuration_raises() -> None:
    """Letting the last entry win would make which registry the bot talks to
    depend on array order — unreadable, and silent."""
    raw = _policy(
        registry_hosts=[
            {"host": "artifactory.corp", "base_url": "https://a.corp"},
            {"host": "artifactory.corp", "base_url": "https://b.corp"},
        ]
    )
    with pytest.raises(ValidationError, match="more than once with different configuration"):
        parse_index_policy(raw)


def test_registries_mapping_is_read_only() -> None:
    """Policy is committed configuration; nothing downstream gets to edit it
    in place."""
    policy = parse_index_policy(_MINIMAL)
    with pytest.raises(TypeError):
        policy.registries["harbor.corp"] = RegistryConfig(host="harbor.corp")  # type: ignore[index]


# --- registry_credential_env ------------------------------------------------


def test_registry_credential_env_names_the_hosts_variable() -> None:
    policy = parse_index_policy(_entry(credentials_env="OCX_REGISTRY_ART"))
    assert registry_credential_env(policy, "oci://artifactory.corp/team/tool") == "OCX_REGISTRY_ART"


def test_registry_credential_env_is_empty_for_an_anonymous_registry() -> None:
    policy = parse_index_policy(_MINIMAL)
    assert registry_credential_env(policy, "oci://ghcr.io/ocx-contrib/cmake") == ""


def test_registry_credential_env_is_empty_for_an_unknown_host() -> None:
    """G-03 has already refused such a repository before any caller reaches
    here; this is the fail-safe answer, not a policy decision."""
    policy = parse_index_policy(_MINIMAL)
    assert registry_credential_env(policy, "oci://elsewhere.corp/team/tool") == ""
    assert registry_credential_env(policy, "not-a-uri") == ""


def test_registry_credential_env_ignores_a_port_in_the_repository() -> None:
    """G-03 matches the port-stripped hostname, so this lookup must too."""
    policy = parse_index_policy(_entry(credentials_env="OCX_REGISTRY_ART"))
    assert (
        registry_credential_env(policy, "oci://artifactory.corp:8443/team/tool")
        == "OCX_REGISTRY_ART"
    )


# --- reserved_namespaces ----------------------------------------------------


def test_reserved_namespaces_defaults_to_empty() -> None:
    """Absent means "this operator reserves nothing extra" — the structural
    set (`p`, `o`, `c`, …) is applied by `core/validate_entry.py` regardless
    and is never expressible here."""
    assert parse_index_policy(_MINIMAL).reserved_namespaces == frozenset()


def test_reserved_namespaces_dedups() -> None:
    raw = _policy(reserved_namespaces=["acme", "admin", "acme"])
    assert parse_index_policy(raw).reserved_namespaces == frozenset({"acme", "admin"})


def test_empty_reserved_namespaces_array_is_legal() -> None:
    """Unlike `registry_hosts`, an empty list here is meaningful and harmless:
    it reserves nothing, which is a coherent policy."""
    assert parse_index_policy(_policy(reserved_namespaces=[])).reserved_namespaces == frozenset()


def test_non_array_reserved_namespaces_raises() -> None:
    with pytest.raises(ValidationError, match="'reserved_namespaces'"):
        parse_index_policy(_policy(reserved_namespaces="acme"))


@pytest.mark.parametrize("entry", ["Acme", "ac me", "acme/tools", "-acme", "", 42])
def test_bad_reserved_namespace_entry_raises(entry: object) -> None:
    """A reserved entry is compared against a namespace segment, so anything
    that cannot *be* one would reserve nothing at all — the silent-no-op bug
    class again."""
    with pytest.raises(ValidationError, match="reserved namespace"):
        parse_index_policy(_policy(reserved_namespaces=[entry]))


# --- governance -------------------------------------------------------------


def test_governance_defaults_to_owners() -> None:
    assert parse_index_policy(_MINIMAL).auto_merge == "owners"


@pytest.mark.parametrize("value", ["owners", "never", "always"])
def test_every_auto_merge_value_parses(value: str) -> None:
    raw = _policy(governance={"auto_merge": value})
    assert parse_index_policy(raw).auto_merge == value


def test_empty_governance_object_keeps_the_default() -> None:
    assert parse_index_policy(_policy(governance={})).auto_merge == "owners"


def test_non_object_governance_raises() -> None:
    with pytest.raises(ValidationError, match="'governance'"):
        parse_index_policy(_policy(governance="owners"))


def test_unknown_governance_key_raises() -> None:
    with pytest.raises(ValidationError, match="unknown key"):
        parse_index_policy(_policy(governance={"automerge": "never"}))


def test_unknown_auto_merge_value_raises() -> None:
    with pytest.raises(ValidationError, match="'auto_merge'"):
        parse_index_policy(_policy(governance={"auto_merge": "yolo"}))


# --- ci ---------------------------------------------------------------------


def test_forge_defaults_to_github() -> None:
    assert parse_index_policy(_MINIMAL).ci.forge == "github"


@pytest.mark.parametrize("value", ["github", "gitlab"])
def test_every_forge_value_parses(value: str) -> None:
    assert parse_index_policy(_policy(ci={"forge": value})).ci.forge == value


def test_empty_ci_object_keeps_the_default() -> None:
    assert parse_index_policy(_policy(ci={})).ci.forge == "github"


def test_non_object_ci_raises() -> None:
    with pytest.raises(ValidationError, match="'ci'"):
        parse_index_policy(_policy(ci="github"))


def test_unknown_ci_key_raises() -> None:
    with pytest.raises(ValidationError, match="unknown key"):
        parse_index_policy(_policy(ci={"packageManager": "bun"}))


def test_unknown_forge_value_raises() -> None:
    with pytest.raises(ValidationError, match="'forge'"):
        parse_index_policy(_policy(ci={"forge": "bitbucket"}))


def test_the_rest_of_the_ci_block_parses() -> None:
    policy = parse_index_policy(
        _policy(
            ci={
                "forge": "gitlab",
                "owner": "acme",
                "run": "uv run --project bot-tools --frozen -- indexbot",
                "setup": "./.github/actions/setup-bot",
                "schedules": {"reconcile": "0 2 * * *", "stale": "30 6 * * *"},
            }
        )
    )
    assert policy.ci.owner == "acme"
    assert policy.ci.run == "uv run --project bot-tools --frozen -- indexbot"
    assert policy.ci.setup == "./.github/actions/setup-bot"
    assert policy.ci.reconcile_cron == "0 2 * * *"
    assert policy.ci.stale_cron == "30 6 * * *"


def test_an_undeclared_owner_is_empty_not_a_guess() -> None:
    """`indexbot ci` refuses to render an unguarded schedule, and it can only
    do that if "not declared" is representable. A default owner would make
    every un-configured index silently claim someone else's name."""
    assert parse_index_policy(_MINIMAL).ci.owner == ""


@pytest.mark.parametrize("key", ["owner", "run", "setup"])
def test_a_non_string_ci_value_raises(key: str) -> None:
    with pytest.raises(ValidationError, match=rf"'ci\.{key}'"):
        parse_index_policy(_policy(ci={key: 42}))


def test_a_non_string_cron_raises() -> None:
    with pytest.raises(ValidationError, match=r"'ci\.reconcile'"):
        parse_index_policy(_policy(ci={"schedules": {"reconcile": True}}))


def test_an_unknown_schedule_key_raises() -> None:
    with pytest.raises(ValidationError, match=r"'ci\.schedules'"):
        parse_index_policy(_policy(ci={"schedules": {"nightly": "0 0 * * *"}}))


def test_a_non_object_schedules_raises() -> None:
    with pytest.raises(ValidationError, match=r"'ci\.schedules'"):
        parse_index_policy(_policy(ci={"schedules": "0 0 * * *"}))


# --- D-7: ci.* values are substituted into privileged YAML, not free text ---


@pytest.mark.parametrize("key", ["owner", "run", "setup"])
@pytest.mark.parametrize("bad", ["acme\ninjected: true", "acme\rinjected: true"])
def test_a_newline_or_cr_in_a_ci_value_raises(key: str, bad: str) -> None:
    """Every `ci.*` string lands verbatim in a generated workflow — one of
    which (`governance.yml`) carries `contents: write` — so a newline would
    let a policy value break out of the line it renders into and inject
    sibling YAML that the drift gate would then report as clean."""
    with pytest.raises(ValidationError, match=rf"'ci\.{key}' must not contain a newline"):
        parse_index_policy(_policy(ci={key: bad}))


@pytest.mark.parametrize("bad", ["acme\ninjected: true", "acme\rinjected: true"])
def test_a_newline_or_cr_in_a_cron_raises(bad: str) -> None:
    """The schedule strings go through the same substitution path as `owner`/
    `run`/`setup` (into `schedule: - cron: "…"`), so they need the same
    newline guard — checked before the cron-shape check runs at all."""
    with pytest.raises(ValidationError, match=r"'ci\.reconcile' must not contain a newline"):
        parse_index_policy(_policy(ci={"schedules": {"reconcile": bad}}))


# --- owner / deploy_workflow: quote- and shell-metacharacter-safe grammars --


@pytest.mark.parametrize("bad", ["acme' && evil", 'acme" || evil'])
def test_a_quote_in_owner_raises(bad: str) -> None:
    """`owner` renders inside a *quoted* scalar — `github.repository_owner ==
    '{{owner}}'` and `$CI_PROJECT_NAMESPACE == "{{owner}}"` — so unlike
    `run`/`setup`, a newline ban is not enough: a `'` or `"` breaks out of
    that scalar and can neutralise the cron-upstream-only guard the value
    itself exists to enforce."""
    with pytest.raises(ValidationError, match=r"'ci\.owner' must be"):
        parse_index_policy(_policy(ci={"owner": bad}))


@pytest.mark.parametrize("owner", ["acme", "acme-org", "acme-org/sub-group", "ocx-sh"])
def test_a_well_formed_owner_parses(owner: str) -> None:
    """A single GitHub-style namespace, or a `/`-separated GitLab subgroup
    path — the two real shapes `owner` is compared against."""
    assert parse_index_policy(_policy(ci={"owner": owner})).ci.owner == owner


@pytest.mark.parametrize("bad", ["Acme", "acme_org", "-acme", "acme-", "acme//sub"])
def test_a_malformed_owner_raises(bad: str) -> None:
    with pytest.raises(ValidationError, match=r"'ci\.owner' must be"):
        parse_index_policy(_policy(ci={"owner": bad}))


@pytest.mark.parametrize(
    "bad", ["../evil.yml", "deploy workflow.yml", "deploy;evil.yml", "sub/deploy.yml"]
)
def test_a_shell_hostile_deploy_workflow_raises(bad: str) -> None:
    """`deploy_workflow` renders *unquoted* into `gh workflow run
    {{deploy_workflow}} --repo "$REPO" --ref main`, in a job holding
    `actions: write` + `contents: write` — a space or shell metacharacter
    would let the value become more than one argv word to `gh`."""
    with pytest.raises(ValidationError, match=r"'ci\.deploy_workflow' must be"):
        parse_index_policy(_policy(ci={"deploy_workflow": bad}))


def test_a_well_formed_deploy_workflow_parses() -> None:
    policy = parse_index_policy(_policy(ci={"deploy_workflow": "render-deploy.yml"}))
    assert policy.ci.deploy_workflow == "render-deploy.yml"


@pytest.mark.parametrize(
    "cron",
    [
        "not-a-cron",
        "* * * *",  # only four fields
        "* * * * * *",  # six fields
        "0 0 * * MON",  # a weekday name, not a shape this grammar admits
    ],
)
def test_a_malformed_cron_raises(cron: str) -> None:
    """A shape check, not a semantic one — enough to stop the value from
    breaking out of the quoted scalar `indexbot ci` renders it into."""
    with pytest.raises(ValidationError, match=r"'ci\.reconcile' must be a 5-field cron"):
        parse_index_policy(_policy(ci={"schedules": {"reconcile": cron}}))


@pytest.mark.parametrize("cron", ["17 3 * * *", "0 5 * * *", "*/15 * * * *", "0 0 1,15 * *"])
def test_a_well_formed_cron_parses(cron: str) -> None:
    assert parse_index_policy(_policy(ci={"schedules": {"reconcile": cron}})).ci.reconcile_cron == (
        cron
    )


# --- ci.run: no default, and a pinned invocation ------------------------------


def test_an_undeclared_run_is_empty_not_a_guess() -> None:
    """The default used to be `uvx ocx-indexbot`, which resolves whatever PyPI
    held when the step started — inside the job that holds `contents: write`
    under `pull_request_target`. An operator who committed the minimal policy
    this package documents got that job, and every gate here called the
    pipeline clean. "Not declared" has to be representable for `indexbot ci`
    to refuse it."""
    assert parse_index_policy(_MINIMAL).ci.run == ""


@pytest.mark.parametrize(
    "command",
    [
        "uvx ocx-indexbot",
        "uvx --from ocx-indexbot indexbot",
        "uv tool run ocx-indexbot",
        # Reads like a lockfile pin and is not one: without `--frozen`/`--locked`,
        # `uv run` re-locks whenever the lockfile is stale against pyproject.toml,
        # and a `[tool.uv.sources]` git source re-locks to a different commit.
        # This was `ocx-sh/index`'s own `ci.run`.
        "uv run --project bot-tools -- indexbot",
        "uv sync && indexbot",
        "pipx run ocx-indexbot",
        "pip install ocx-indexbot && indexbot",
        # A mutable git ref is the same hazard wearing a commit's clothes.
        "uvx --from git+https://github.com/ocx-sh/indexbot@main indexbot",
    ],
)
def test_a_runtime_resolved_invocation_is_recognised(command: str) -> None:
    assert resolves_at_runtime(command)


@pytest.mark.parametrize(
    "command",
    [
        "uv run --project bot-tools --frozen -- indexbot",
        "uv run --locked --project bot-tools -- indexbot",
        "uvx --from 'ocx-indexbot==0.2.0' indexbot",
        "uvx ocx-indexbot@0.2.0",
        "python -m pip install ocx-indexbot==0.2.0 && indexbot",
        "uvx --from git+https://github.com/ocx-sh/indexbot@" + "a" * 40 + " indexbot",
        # Names no resolver at all: the operator's own image, container
        # entrypoint or vendored wheel decides the version, and this package is
        # in no position to second-guess how. Refusing these is what would turn
        # "any index, any forge" into "any index that uses uv".
        "indexbot",
        "/opt/indexbot/bin/indexbot",
        "docker run --rm acme/indexbot@sha256:abc indexbot",
        "poetry run indexbot",
    ],
)
def test_a_pinned_or_resolver_free_invocation_is_accepted(command: str) -> None:
    assert not resolves_at_runtime(command)
