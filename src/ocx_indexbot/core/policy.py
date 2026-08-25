"""`.github/index-policy.json` parsing — this deployment's own identity.

OCX's index is "one format, many copies": the public `ocx-sh/index` and any
corporate copy run the same bot over the same wire format but share almost no
configuration — a corporate operator's physical bytes live on their
Harbor/Artifactory/ECR, their packages are named under *their* prefix, and
their governance lane may run on a different forge entirely. Everything that
differs per copy is read from a file each index repository commits for
itself, never a constant compiled into this package.

**Why a committed file and not an environment/Actions variable.** The
allowlist's whole control is "extend only via reviewed PR" — it is the set of
hosts every ocx client will follow to fetch bytes, so widening it is a
supply-chain trust decision. An env var or `vars.`/`secrets.` entry can be
changed by anyone with repo-settings access, silently, with no diff and no
reviewer. A committed file keeps the property mechanical: widening trust *is*
a PR, and `.github/**` is exactly the surface branch protection and CODEOWNERS
already guard. `.github/` is also where this repo already keeps its other
bot-read, PR-reviewed governance data (`maintainers.yml`, G-20). The same
argument now covers `name` and `name_segments`, which decide what every
published root is *called*.

**`name` and `name_segments` are required, with no defaults.** A default of
`ocx.sh`/`2` is precisely the hardcode 0.2.0 exists to remove; leaving one in
place would mean every index that forgot to declare its identity silently
published under OCX's. There is no compatibility shim for v1 files — the one
deployment that predates this is `ocx-sh/index`, which we control.

**Why a JSON Schema exists but is not consulted here.** `schema/*.schema.json`
in an index repository is the *served* wire contract (`$id:
https://index.ocx.sh/schema/...`), sealed by
`adr_locked_observation_index_format.md` D7; this file is a bot-side
deployment artifact that is never served and never part of the index format.
`ocx_indexbot/schema/index-policy-v1.schema.json` ships in the wheel purely so
an operator's editor can autocomplete the file — `parse_index_policy` below is
the runtime authority, because ADR-4 BD-1 keeps `httpx` the only runtime
dependency and a schema validator is not one. The two are kept honest by a
shared fixture corpus (`tests/fixtures/policy/`), not by one calling the
other.

JSON (stdlib `json`), not YAML: unlike `core/maintainers.py`'s and
`cli/seed_import.py`'s hand-rolled `key: value` readers, this needs no parser
of its own at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final, Literal, cast, get_args

from ocx_indexbot.core.grammar import HOST_MAX_LENGTH, HOST_RE, NAMESPACE_RE, NAMESPACE_SHAPE
from ocx_indexbot.errors import ValidationError

INDEX_POLICY_PATH: Final[str] = ".github/index-policy.json"
"""Repo-relative path of the policy file, read through a `FilePort` (local
checkout) or a `ForgePort` at `main` (`cli/announce.py`'s publisher lane runs
outside any index checkout — see `cli/_wiring.py`)."""

MAX_NAME_SEGMENTS: Final[int] = 8
"""Upper bound on `name_segments`.

Not an opinion about how deep a package hierarchy should be — it exists so
every derived path length stays bounded. `core/validate_entry.py` computes a
package id's maximum length, and `cli/classify_pr.py` its CAS path's, from
this declaration; without a ceiling both become unbounded and the
length-cap-before-regex order BD-4 requires has nothing to cap against.
"""

AutoMerge = Literal["owners", "never", "always"]
Forge = Literal["github", "gitlab"]

_RUNTIME_RESOLVER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\s;&|(])(?:uvx|uv\s+(?:tool\s+run|run|sync)|pipx\s+(?:run|install)"
    r"|(?:python[\d.]*\s+-m\s+)?pip[\d]*\s+install)(?![\w-])"
)
"""The package managers that decide, at job start, which version to fetch."""

_PINNED_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)--(?:frozen|locked)(?![\w-])|==|@(?:[0-9a-f]{40}|\d[\w.+!-]*)(?![\w])"
)
"""What makes one of them deterministic: a lockfile it may not re-resolve
(`--frozen`, `--locked`), an exact version specifier (`==1.2.3`), or a ref
that cannot move (`@<40-hex>`, `@1.2.3`). A tag-shaped ref must start with a
digit — `@main` and `@latest` are exactly what this must not bless."""

DEFAULT_RECONCILE_CRON: Final[str] = "17 3 * * *"
DEFAULT_STALE_CRON: Final[str] = "0 5 * * *"
_SCHEDULE_KEYS: Final[frozenset[str]] = frozenset({"reconcile", "stale"})

_CRON_FIELD: Final[str] = r"[0-9*,/-]+"
_CRON_RE: Final[re.Pattern[str]] = re.compile(rf"^{_CRON_FIELD}(?: {_CRON_FIELD}){{4}}$")
"""A shape check, not a semantic one — five space-separated fields, each
drawn from digits and cron's own punctuation. `indexbot ci` substitutes this
value verbatim into a generated `schedule: - cron: "…"` line, so the job here
is only ever "can this break out of that quoted scalar", never "is this a
schedule that will actually fire"."""

_OWNER_RE: Final[re.Pattern[str]] = re.compile(rf"{NAMESPACE_SHAPE}(?:/{NAMESPACE_SHAPE})*")
"""`ci.owner`'s grammar — reuses `NAMESPACE_SHAPE` (a package namespace
segment) rather than inventing a new alphabet, composed for one `/` per
GitLab subgroup: GitHub's `repository_owner` is always a single segment, but
GitLab's `$CI_PROJECT_NAMESPACE` can be `group/subgroup`.

`indexbot ci` substitutes `owner` inside a *quoted* scalar —
`github.repository_owner == '{{owner}}'` (an expression string literal) and
`$CI_PROJECT_NAMESPACE == "{{owner}}"` (a GitLab rule expression) — unlike
`ci.run`, which IS the shell command a step runs and so has nothing to break
out of. `ci.run` carries a different constraint instead, and a sharper one:
`resolves_at_runtime` below.
`_require_string`'s newline check stops a value from breaking out of the
*line*, but a `'` or `"` inside the value still breaks out of the *scalar*
it renders into and lets the rest inject sibling expression syntax into the
cron-upstream-only guard every scheduled job carries. This grammar has no
quote, space or backslash in its alphabet, so it cannot end a scalar it did
not start."""

_DEPLOY_WORKFLOW_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]+")
"""`ci.deploy_workflow`'s grammar: a bare workflow filename, nothing a path
component could hide in. `governance-deploy-job.yml` substitutes it
*unquoted* into a shell command — `gh workflow run {{deploy_workflow}}
--repo "$REPO" --ref main` — in a job that holds `actions: write` +
`contents: write` (arming the merge already happened; this job only
dispatches). Unquoted means space-separated words in the value become
separate argv entries to `gh`, so this grammar excludes whitespace and shell
metacharacters outright rather than relying on the template to quote it
correctly forever. The call site is quoted too, belt-and-suspenders, but the
grammar is what makes that quoting inert rather than load-bearing."""

_AUTO_MERGE_VALUES: Final[frozenset[str]] = frozenset(get_args(AutoMerge))
FORGE_VALUES: Final[frozenset[str]] = frozenset(get_args(Forge))
"""Derived from the `Literal`s themselves, never restated.

Each member used to be written out three times — the type, the parser's
accept-set, and `cli/announce.py`'s `--forge` choices. A fourth forge added
to the type but not to a hand-written set type-checks clean and is rejected
at runtime, which is the worst way to find out.
"""

_SCHEMA_KEY: Final[str] = "$schema"
_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
    {
        _SCHEMA_KEY,
        "name",
        "name_segments",
        "registry_hosts",
        "reserved_namespaces",
        "governance",
        "ci",
    }
)


@dataclass(frozen=True, slots=True)
class CiConfig:
    """The inputs `indexbot ci` renders a pipeline from.

    Deliberately four strings and two cron expressions, not a workflow DSL.
    Everything else a generated file contains is the bot's own governance
    logic, which is the same for every deployment and is exactly why it is
    generated rather than copy-pasted. A knob here has to be something no
    index can inherit from another: who owns the repository, how this
    deployment invokes the bot, and when its schedules fire.

    `forge` is **not** how the bot decides which API to talk to at run time —
    that comes from the runner's own variables (`cli/_wiring._forge_api`),
    because the privileged subcommands read this file *through* the port it
    would otherwise be choosing. It says which pipeline files to render, and
    only that.
    """

    forge: Forge
    """Which pipeline files `indexbot ci` renders: GitHub Actions workflows,
    or the GitLab CI job file a root `.gitlab-ci.yml` includes."""

    owner: str
    """The repository owner / GitLab namespace, for the cron-upstream-only
    guard every scheduled job carries.

    Empty means "not declared", which `indexbot ci` refuses rather than
    guesses: a fork inherits every schedule in a workflow file and would run
    it against its own stale copy, so the guard is a security invariant and
    an unguarded generated schedule would be a generator that emits an
    invariant violation.
    """

    run: str
    """The command a generated job uses to invoke the bot.

    Empty means "not declared", which `indexbot ci` refuses rather than
    guesses — the same shape as `owner` above, for a sharper reason. The
    default used to be `uvx ocx-indexbot`: zero-setup, and it resolved
    whatever PyPI held at job start, inside the one job that holds
    `contents: write` under `pull_request_target`. A deployment that never
    thought about this key got a privileged job running that morning's
    release. There is no spelling of a default that is both zero-setup and
    pinned, so there is no default — see `resolves_at_runtime`.
    """

    setup: str
    """A step that installs whatever `run` needs, rendered as a `uses:` on
    GitHub (typically a local composite action) and as an `image:` on GitLab.
    Empty renders no setup step at all."""

    deploy_workflow: str
    """The workflow file a merged machine-lane PR should dispatch, if this
    deployment has one. Empty renders no such job at all — publishing is a
    hosting choice (Cloudflare Pages, GitLab Pages, an internal mirror), and a
    generator that named one would make every index deploy the way the public
    one happens to."""

    reconcile_cron: str
    stale_cron: str
    """GitHub only. GitLab schedules are project settings rather than pipeline
    config, so a generated `.gitlab-ci` file names no cron: it distinguishes
    the two scheduled lanes by a variable the schedule carries."""


@dataclass(frozen=True, slots=True)
class IndexPolicy:
    """One index deployment's committed configuration.

    Everything here is per-copy. Nothing here is wire contract: the served
    URL shapes are frozen for every index alike, and this decides only what
    *this* index puts in them.
    """

    name: str
    """Logical prefix every `root.name` carries — `ocx.sh` for the public
    index, `acme.corp` for a corporate one. Also the registry-namespace key
    an ocx client configures (`[registries."<name>"] index = …`)."""

    name_segments: int
    """How many `/`-separated segments follow `name` in a package id, and so
    how deep `p/**` nests. Published in the rendered `config.json` because no
    client can derive it from a tree."""

    registry_hosts: frozenset[str]
    """G-03's allowlist: hosts whose `oci://` repositories this index will
    admit. Narrowed further at wiring time to hosts some `RegistryPort` can
    actually serve."""

    reserved_namespaces: frozenset[str]
    """Operator-reserved first segments — a brand, an internal prefix. The
    *structural* reservations (`p`, `o`, `c`, `config`, …) are not expressible
    here: they follow from the served URL shapes and hold for every index, so
    `core/validate_entry.py` applies them unconditionally."""

    auto_merge: AutoMerge
    """`owners` — machine-lane PRs auto-merge when the author owns every
    touched root (the public index's policy). `never` — every PR waits for a
    human. `always` — any machine-lane PR auto-merges."""

    ci: CiConfig
    """What `indexbot ci` renders, and for which forge."""


def root_glob(name_segments: int) -> str:
    """The git pathspec that selects exactly a package root and never a CAS
    object: one `*` per declared segment, then `*.json`.

    `:(glob)` at the call site is what makes each `*` stop at a `/` — a git
    pathspec is not a shell glob, and without that magic prefix `*` matches
    `/` too, so `p/*/*.json` also selects every `p/<ns>/<pkg>/o/sha256/<hex>.json`
    a PR adds. Since every announce adds a CAS object, that turned the
    required validation check red on every announce PR.

    Lives here rather than in `ci/render.py` because two consumers must agree
    on it exactly: the generated pipeline (which interpolates it into a
    `git diff` pathspec) and `cli/validate_pr.py` (which runs that same diff
    itself). A second copy would drift the moment `name_segments` changed.
    """
    return "p/" + "*/" * (name_segments - 1) + "*.json"


def resolves_at_runtime(command: str) -> bool:
    """Whether `command` asks a package manager to decide, at job start, which
    version of the bot to run.

    One predicate behind two refusals that used to be one hole: `indexbot ci`
    will not render a pipeline whose `ci.run` matches it, and WF-08
    (`core/workflow_invariants.py`) will not pass a `contents: write` job
    under `pull_request_target` whose `run:` does. Both exist because that job
    holds a token that can move an unprotected base branch and squash-merge a
    pull request, and until 0.2.0 the *documented default* for `ci.run` was
    `uvx ocx-indexbot` — one malicious release away from executing there,
    with every gate in this package reporting the pipeline clean.

    **Why a denylist of resolvers, not a grammar of pinned shapes.** "Pinned"
    is not decidable from a string in general: `/opt/indexbot/bin/indexbot` is
    pinned by the image that contains it, `docker run acme/bot@sha256:…` by
    its digest, and a corporate operator may reach the bot through Poetry,
    Hatch, Nix or a vendored wheel. An allowlist of blessed spellings would
    refuse every one of those and force a fork of this package, which is the
    opposite of what "any index, any forge" exists for. So the rule is
    narrower and says only what it can prove: a command that *names a resolver
    this package recognises* must also carry the flag that makes that resolver
    deterministic. A command that names none is the operator's own image, and
    out of scope.

    The gap that leaves is stated rather than papered over — a resolver this
    list does not know (`pdm run`, a `curl | sh` bootstrap) passes. What it
    catches is the shape an operator copies out of a README, which is exactly
    how the hole got here.

    **Why `uv run` counts as a resolver.** Without `--frozen` or `--locked` it
    re-locks whenever the lockfile is stale against `pyproject.toml`, and with
    a `[tool.uv.sources]` git source re-locking moves the commit. So
    `uv run --project bot-tools -- indexbot` — which reads like a lockfile pin,
    and was `ocx-sh/index`'s own `ci.run` — is not one. The flag is what makes
    the lockfile binding, which is why the floating default and the missing
    `--frozen` are one fix and not two.

    Applied per LINE by WF-08, never to a whole job block: two `run:` steps in
    one job, one pinned and one not, would otherwise let the pinned one's flag
    vouch for its neighbour. The cost is that a command folded across lines
    (`run: >-`) reads as unpinned; the generated templates deliberately do not
    fold, and erring toward a finding is the right direction here.
    """
    return _RUNTIME_RESOLVER_RE.search(command) is not None and _PINNED_RE.search(command) is None


def _require_object(value: Any, key: str) -> dict[str, Any]:  # noqa: ANN401 — raw JSON
    if not isinstance(value, dict):
        raise ValidationError(f"{INDEX_POLICY_PATH}: {key!r} must be a JSON object")
    return cast("dict[str, Any]", value)


def _reject_unknown(data: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: unknown key(s) {unknown} in {where} — supported: "
            f"{sorted(allowed)}"
        )


def _parse_name(data: dict[str, Any]) -> str:
    if "name" not in data:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: missing required 'name' — the logical prefix every "
            "published root carries. There is no default: an index that does not declare "
            "its own name would publish under another deployment's."
        )
    name: Any = data["name"]
    if not isinstance(name, str) or len(name) > HOST_MAX_LENGTH:
        raise ValidationError(f"{INDEX_POLICY_PATH}: 'name' must be a bare lowercase host")
    if HOST_RE.fullmatch(name) is None:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: 'name' {name!r} must be a bare lowercase host "
            "(no scheme, no port, no path) — it is the registry key an ocx client "
            'configures as [registries."<name>"]'
        )
    return name


def _parse_name_segments(data: dict[str, Any]) -> int:
    if "name_segments" not in data:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: missing required 'name_segments' — how many segments a "
            "package id has under this index. There is no default."
        )
    segments: Any = data["name_segments"]
    # `isinstance(True, int)` is True, so bool must be excluded explicitly or
    # `true` would silently mean 1.
    if isinstance(segments, bool) or not isinstance(segments, int):
        raise ValidationError(f"{INDEX_POLICY_PATH}: 'name_segments' must be an integer")
    if not 1 <= segments <= MAX_NAME_SEGMENTS:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: 'name_segments' must be between 1 and {MAX_NAME_SEGMENTS}, "
            f"got {segments}"
        )
    return segments


def _parse_registry_hosts(data: dict[str, Any]) -> frozenset[str]:
    if "registry_hosts" not in data:
        raise ValidationError(f"{INDEX_POLICY_PATH}: missing required 'registry_hosts' key")
    hosts_raw: Any = data["registry_hosts"]
    if not isinstance(hosts_raw, list):
        raise ValidationError(f"{INDEX_POLICY_PATH}: 'registry_hosts' must be an array of hosts")
    hosts = cast("list[Any]", hosts_raw)
    if not hosts:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: 'registry_hosts' must list at least one host — an empty "
            "allowlist rejects every package root this index could ever serve"
        )
    for host in hosts:
        if not isinstance(host, str) or len(host) > HOST_MAX_LENGTH:
            raise ValidationError(f"{INDEX_POLICY_PATH}: {host!r} is not a registry host")
        if HOST_RE.fullmatch(host) is None:
            raise ValidationError(
                f"{INDEX_POLICY_PATH}: {host!r} is not a bare lowercase registry host "
                "(no scheme, no port, no path)"
            )
    return frozenset(cast("list[str]", hosts))


def _parse_reserved_namespaces(data: dict[str, Any]) -> frozenset[str]:
    raw: Any = data.get("reserved_namespaces", [])
    if not isinstance(raw, list):
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: 'reserved_namespaces' must be an array of segments"
        )
    entries = cast("list[Any]", raw)
    for entry in entries:
        if not isinstance(entry, str) or NAMESPACE_RE.fullmatch(entry) is None:
            raise ValidationError(
                f"{INDEX_POLICY_PATH}: {entry!r} is not a reserved namespace — an entry is "
                "compared against a first path segment, so anything that cannot be one "
                "would reserve nothing at all"
            )
    return frozenset(cast("list[str]", entries))


def _parse_governance(data: dict[str, Any]) -> AutoMerge:
    block = _require_object(data.get("governance", {}), "governance")
    _reject_unknown(block, frozenset({"auto_merge"}), "'governance'")
    value: Any = block.get("auto_merge", "owners")
    if value not in _AUTO_MERGE_VALUES:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: 'auto_merge' must be one of {sorted(_AUTO_MERGE_VALUES)}, "
            f"got {value!r}"
        )
    return cast("AutoMerge", value)


def _require_string(block: dict[str, Any], key: str, default: str) -> str:
    """Every `ci.*` value returned here is substituted line-by-line into a
    generated workflow — into a `run:` block, a `uses:`/`image:` line, a
    cron-upstream-only `if:`, or a `schedule:` entry — one of which
    (`governance.yml`) runs with `contents: write`. A newline in any of them
    would break the line it lands on and let the rest of its value inject
    sibling YAML into that file, and the drift gate would then report the
    injected result as clean, because it renders byte-for-byte from this same
    string. So the type check and the newline check both happen here, at the
    one place every consumer parses through — never at render time, by which
    point the value has already been trusted."""
    value: Any = block.get(key, default)
    if not isinstance(value, str):
        raise ValidationError(f"{INDEX_POLICY_PATH}: 'ci.{key}' must be a string, got {value!r}")
    if "\n" in value or "\r" in value:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: 'ci.{key}' must not contain a newline — it is substituted "
            "directly into a generated workflow file, and a newline would let it inject "
            "sibling YAML into a job that holds write-scoped permissions"
        )
    return value


def _require_pattern(block: dict[str, Any], key: str, pattern: re.Pattern[str], shape: str) -> str:
    """Like `_require_string`, but for a `ci.*` value substituted somewhere
    more hostile than free text: `owner` into a quoted expression scalar,
    `deploy_workflow` unquoted into a shell command (see `_OWNER_RE` and
    `_DEPLOY_WORKFLOW_RE`). `_require_string`'s newline check stops a line
    break; it says nothing about the quote or shell-metacharacter a scalar or
    an unquoted word can still be broken out of, which is what `pattern`
    closes.

    Both current callers default to `""` ("not declared") and both already
    give that case its own meaning downstream (`indexbot ci` refuses to
    render an unguarded schedule without `owner`; an empty `deploy_workflow`
    just renders no deploy job) — so empty is exempt from `pattern` here
    rather than every caller re-deriving the same exemption.
    """
    value = _require_string(block, key, "")
    if value and pattern.fullmatch(value) is None:
        raise ValidationError(f"{INDEX_POLICY_PATH}: 'ci.{key}' must be {shape}, got {value!r}")
    return value


def _require_cron(schedules: dict[str, Any], key: str, default: str) -> str:
    value = _require_string(schedules, key, default)
    if _CRON_RE.fullmatch(value) is None:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: 'ci.{key}' must be a 5-field cron expression "
            f"(minute hour day month weekday), got {value!r}"
        )
    return value


def _parse_schedules(block: dict[str, Any]) -> tuple[str, str]:
    schedules = _require_object(block.get("schedules", {}), "ci.schedules")
    _reject_unknown(schedules, _SCHEDULE_KEYS, "'ci.schedules'")
    return (
        _require_cron(schedules, "reconcile", DEFAULT_RECONCILE_CRON),
        _require_cron(schedules, "stale", DEFAULT_STALE_CRON),
    )


def _parse_ci(data: dict[str, Any]) -> CiConfig:
    block = _require_object(data.get("ci", {}), "ci")
    _reject_unknown(
        block,
        frozenset({"forge", "owner", "run", "setup", "deploy_workflow", "schedules"}),
        "'ci'",
    )
    forge: Any = block.get("forge", "github")
    if forge not in FORGE_VALUES:
        raise ValidationError(
            f"{INDEX_POLICY_PATH}: 'forge' must be one of {sorted(FORGE_VALUES)}, got {forge!r}"
        )
    reconcile_cron, stale_cron = _parse_schedules(block)
    return CiConfig(
        forge=cast("Forge", forge),
        owner=_require_pattern(
            block,
            "owner",
            _OWNER_RE,
            "a bare namespace, or a GitLab subgroup path (letters, digits, single hyphens, '/')",
        ),
        # `run` IS the shell command a step runs, so — unlike `owner` and
        # `deploy_workflow` above/below — nothing renders it inside a scalar
        # or a shell word it could break out of, and the newline-only check
        # `_require_string` gives every ci.* value is the whole of its
        # *injection* grammar. Its pinned-invocation refusal lives at RENDER
        # time (`ci/render.build_render_plan`) rather than here, for the same
        # reason `owner`'s does: a policy with no `ci` block at all is legal
        # for every subcommand that renders no pipeline, and `announce` on a
        # publisher's laptop must not start failing over a CI-only key.
        run=_require_string(block, "run", ""),
        setup=_require_string(block, "setup", ""),
        deploy_workflow=_require_pattern(
            block,
            "deploy_workflow",
            _DEPLOY_WORKFLOW_RE,
            "a bare workflow filename (letters, digits, '.', '_', '-')",
        ),
        reconcile_cron=reconcile_cron,
        stale_cron=stale_cron,
    )


def parse_index_policy(raw: bytes) -> IndexPolicy:
    """Parse `.github/index-policy.json` bytes into an `IndexPolicy`.

    Raises `ValidationError` on anything the grammar does not admit. Every
    rejection here is a shape that would otherwise fail *silently* — a typo'd
    key leaving a deployment with no policy while looking like it had one, a
    scheme-prefixed host that allowlists nothing, a reserved entry that can
    never match a real segment.

    Duplicates within an array are not an error; the results are frozensets.
    """
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{INDEX_POLICY_PATH}: malformed JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"{INDEX_POLICY_PATH}: must be a JSON object")
    data = cast("dict[str, Any]", parsed)

    # `$schema` is accepted and ignored — it exists so an operator's editor can
    # autocomplete the file, and nothing validates against it at runtime.
    _reject_unknown(data, _TOP_LEVEL_KEYS, "the document")

    return IndexPolicy(
        name=_parse_name(data),
        name_segments=_parse_name_segments(data),
        registry_hosts=_parse_registry_hosts(data),
        reserved_namespaces=_parse_reserved_namespaces(data),
        auto_merge=_parse_governance(data),
        ci=_parse_ci(data),
    )
