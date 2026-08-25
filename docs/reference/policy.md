# Deployment policy

`.github/index-policy.json`, at the root of the *index* repository — not of
this package. One index format, many copies: the public index serves bytes
from `ghcr.io`, a corporate copy from its own Harbor, Artifactory or ECR.
Each index states its own.

```json
{
  "$schema": "https://ocx-sh.github.io/indexbot/schema/index-policy-v1.schema.json",
  "name": "acme.corp",
  "name_segments": 2,
  "registry_hosts": ["harbor.corp"],
  "reserved_namespaces": ["acme"],
  "governance": { "auto_merge": "owners" },
  "ci": {
    "forge": "gitlab",
    "owner": "acme",
    "run": "uv run --project bot-tools --frozen -- indexbot",
    "setup": "./.github/actions/setup-bot"
  }
}
```

Anything outside this grammar is a hard error: malformed JSON, a non-object
document, an unknown key at any level, a missing required key, or a value
that cannot be what it claims to be.

| Key | Required | Default | Meaning |
|---|---|---|---|
| `name` | yes | — | Logical prefix every published root carries. `acme.corp/team/tool` |
| `name_segments` | yes | — | Segments after the prefix, 1–8. Published in the rendered `config.json` |
| `registry_hosts` | yes | — | The registries this index admits and how to reach them — a bare host string, or an object (below) |
| `reserved_namespaces` | no | none | First segments this operator reserves |
| `governance.auto_merge` | no | `owners` | `owners` \| `never` \| `always` — see [`governance-check`](cli.md#governance-check) |
| `ci.forge` | no | `github` | `github` \| `gitlab` — which pipeline files [`indexbot ci`](cli.md#ci) renders |
| `ci.owner` | for `indexbot ci` | — | Repository owner / GitLab namespace, for the cron-upstream-only guard |
| `ci.run` | for `indexbot ci` | — | How a generated job invokes the bot. Must not resolve the version at job start — see below |
| `ci.setup` | no | none | A step that installs what `run` needs — a `uses:` target on GitHub, an `image:` on GitLab |
| `ci.deploy_workflow` | no | none | The workflow a merged machine-lane PR dispatches. Empty renders no such job |
| `ci.schedules.reconcile` | no | `17 3 * * *` | GitHub only |
| `ci.schedules.stale` | no | `0 5 * * *` | GitHub only |

## Why `ci` is not how the bot picks a forge

`ci.forge` says which pipeline files to render. It is **not** read to decide
which API to talk to at run time: that comes from the runner's own variables
(`$GITLAB_CI`). The privileged subcommands read this very file *through* a
forge port, so a policy-derived port choice would need the port it was
choosing.

`ci.owner` has no default because every generated schedule carries a
cron-upstream-only guard keyed on it. A fork inherits every schedule in a
workflow file and would run it against its own stale copy, so `indexbot ci`
refuses to render rather than guess a name.

## Why `ci.run` has no default, and must be pinned

`ci.run` used to default to `uvx ocx-indexbot`. It is the command *every*
generated job runs, and on GitHub one of those jobs — `arm-auto-merge` — holds
`contents: write` under `pull_request_target`, a token that can move an
unprotected base branch and squash-merge a pull request. `uvx ocx-indexbot`
fetches the latest release when the step starts: no version, no lockfile, no
hash. So an operator who committed the minimal policy above got a privileged
job running whatever was published that morning, and every gate this package
ships reported the pipeline clean.

There is no spelling of a default that is both zero-setup and pinned, so there
is no default. `indexbot ci` refuses to render without one, and refuses a value
it can see resolving at job start:

```
ci.run 'uvx ocx-indexbot' resolves the bot at job start. …
```

Any of these is accepted:

| Shape | Why it is pinned |
|---|---|
| `uv run --project bot-tools --frozen -- indexbot` | the lockfile binds, and `--frozen` forbids re-resolving it |
| `uv run --locked --project bot-tools -- indexbot` | the same, and it fails if the lock is stale |
| `uvx --from 'ocx-indexbot==0.2.0' indexbot` | an exact version specifier |
| `uvx --from git+https://…@<40-hex> indexbot` | a ref that cannot move |
| `indexbot`, `/opt/indexbot/bin/indexbot`, `poetry run indexbot` | names no resolver — your image or your vendored install decided the version, and this package is in no position to second-guess how |

`--frozen` is not decoration. Without it — or `--locked` — `uv run` re-locks
whenever the lockfile is stale against `pyproject.toml`, and re-locking a
`[tool.uv.sources]` git source moves the commit. `uv run --project bot-tools
-- indexbot` reads like a lockfile pin and is not one.

What this **cannot** check is a resolver it does not recognise, or one hidden
inside a `ci.setup` composite action. The rule refuses the shapes an operator
copies out of a README; keeping your own bootstrap pinned is yours to do.
[WF-08](workflow-invariants.md) applies the same predicate to the rendered
tree, so a hand-written pipeline answers to it too.

## Why `name` and `name_segments` have no defaults

They are the index's identity. A default of `ocx.sh`/`2` would mean an index
that forgot to declare itself silently publishes under OCX's name, which is
exactly the hardcode this file exists to remove. Declaring them is one line
each and it is not optional.

`name` is also the registry key an ocx client configures — `[registries."acme.corp"]
index = "https://index.acme.corp"` — which is why it carries the registry-host
grammar rather than a looser one. It is a *logical* name and need not equal the
host serving it.

`name_segments` is an operator declaration no tree can be read for: a client
that finds `p/a/b/c.json` cannot tell a three-segment index from a
two-segment one holding a package called `b/c`. Publishing it lets a client
resolve a name of the wrong shape as plain OCI instead of reading the
unavoidable 404 as an authoritative refusal.

## What is *not* configurable here

The **structural** reservations — `p`, `o`, `c`, `config`, `schema`, `docs`,
`assets`, `api`, `static`, `data`, `index` — follow from the served URL
shapes and hold for every index alike, so the bot applies them
unconditionally and `reserved_namespaces` cannot express them. That key is
for what *this* operator additionally wants held back: a brand, an internal
prefix.

## The schema

The grammar above ships as a JSON Schema inside the wheel. Point your editor
at the published copy for autocomplete, or pin the one your CI actually runs:

```bash
indexbot schema > .github/index-policy.schema.json
```

Nothing validates against it at runtime — the parser is the authority, and the
bot carries no schema validator (its only runtime dependency is `httpx`). The
two are held together by a shared fixture corpus in the package's test suite:
every accepted example must be accepted by both, every rejected one rejected
by both.

## Registries

A `registry_hosts` entry is either a bare host string or an object. The entry
*is* the client's configuration: allowlisting a host and being able to fetch
from it are the same statement, so there is no such thing as a host this bot
admits and cannot reach.

| Field | Required | Default | Meaning |
|---|---|---|---|
| `host` | yes | — | The bare host, exactly as it appears in `oci://<host>/…`. No scheme, no port |
| `base_url` | no | `https://<host>` | Where the Registry v2 API actually lives — scheme, port and path prefix |
| `realm` | no | `<base_url>/token` | The token endpoint, for `auth: "token"` |
| `service` | no | `host` | The `service` parameter the realm expects |
| `auth` | no | `token` | `token` \| `basic` — see below |
| `credentials_env` | no | none | Name of the environment variable holding `user:password`. Absent → anonymous |

A bare string is `{"host": "…"}` with every default taken, which is why
`["ghcr.io"]` keeps meaning what it always meant.

### The two auth flows

`token` is the Docker/OCI dance: the bot asks the configured `realm` for a
`repository:<path>:pull` token — authenticating that one request with the
credential, if there is one — and sends the short-lived Bearer it gets back to
`/v2/`. This is ghcr.io, GitLab, Harbor, Artifactory's OCI endpoints.

`basic` sends RFC 7617 credentials on every `/v2/` request and never asks for
a token. Some Nexus and ECR-alike deployments answer that way; there is no
realm to ask.

The realm is **configured, never discovered**. A `401` carries a
`WWW-Authenticate` header naming a realm, and following it would let whatever
answers `base_url` choose the address the operator's credential is sent to.
This bot ignores that header. Same reason a credential is dropped on any
cross-origin redirect.

### Anonymous stays the default

No `credentials_env` means the exact code path a public index has always run:
no `Authorization` header is built at all. Public registries are first-class,
not a degraded case of the private one.

### Built-in defaults

Three hosts carry facts no convention supplies, so a bare entry for them is
enough:

| Host | What is filled in |
|---|---|
| `ghcr.io` | nothing — Registry v2 defaults are correct |
| `ocx.sh` | its Artifactory token path, not `https://ocx.sh/token`, which 404s |
| `registry.gitlab.com` | realm `https://gitlab.com/jwt/auth`, `service` the literal `container_registry`, **not** the host |

An entry that states its own `base_url` is not given these. Repointing
`ocx.sh` at an internal mirror and inheriting the public realm would send that
mirror's credentials to an address nobody wrote down.

### A private registry, end to end

```json
"registry_hosts": [
  "ghcr.io",
  {
    "host": "artifactory.corp",
    "base_url": "https://oci-prod.artifactory.corp:8443",
    "realm": "https://oci-prod.artifactory.corp:8443/v2/token",
    "auth": "token",
    "credentials_env": "OCX_REGISTRY_ARTIFACTORY"
  }
]
```

The **name** is committed and reviewed. The **value** never is: set it as a
repository secret (GitHub — [`indexbot ci`](cli.md#ci) renders it into the
`reconcile` job's `env:` for you) or a masked, protected CI/CD variable
(GitLab, ambient exactly like `$GITLAB_TOKEN`).

### Which lanes hold it

| Lane | Credential | What it verifies |
|---|---|---|
| `validate` / `validate-pr` (`pull_request`, `merge_request_event`) | never | Shape and bytes. For a credentialed host, registry checks are **skipped with a WARN naming the variable** |
| `reconcile`, `seed-import` (privileged) | yes — **refuses to start without it** | Registry truth, including every root the fork lane could only shape-check |

A fork's pipeline holds no secret; that is the privileged/unprivileged split
working as designed, not a gap. What would be a gap is skipping silently, so
the skip is printed per root and the covering lane fails loudly rather than
running anonymous.

That the fork lane holds no credential is a *construction*, not a
configuration: its registry clients are built from an empty environment. This
matters because `validate` reads the policy out of pull-request-head content,
and an entry names both where a credential is sent (`base_url`) and which
variable holds it — a pair a pull request must never get to choose.

## Why the host shape is strict

The allowlist is matched against a URL's parsed `hostname`, which is always
lowercased and never carries a port. So `https://harbor.corp`, `Harbor.Corp`
and `harbor.corp:5000` would each parse happily as policy entries and then
match nothing at all — an allowlist that silently admits no one. Rejecting
them at parse time is the difference between a loud error and an index where
every download fails for a reason nobody can see.

A registry on a non-standard port is allowlisted by its bare host:
`harbor.corp` admits `oci://harbor.corp:5000/team/tool`. The port belongs in
`base_url`, which is where the bot reads it from.

## Why it is a committed file

**Never an environment variable, a repository variable or an Actions
variable.** `repository` is the pointer every client follows to fetch bytes,
so widening the allowlist is a supply-chain trust decision. "Extend only via
reviewed pull request" *is* the control — and a settings-page value can be
changed by anyone with settings access, silently, with no diff and no
reviewer.

Keeping it under `.github/**` also puts it on the same surface branch
protection and CODEOWNERS already guard, beside the other governance data the
bot reads.

## Fail closed on a missing policy

An index copy that never stated a policy says so at startup, rather than
inheriting the public index's hosts by accident.

There used to be a second early failure here — "a host no adapter can serve" —
because the servable hosts were a compiled-in triple and allowlisting
`harbor.corp.internal` produced roots that validated and then could not be
fetched. It has nothing left to guard: the allowlist entry is the client's
configuration, so a host cannot be admitted and unreachable at the same time.

## The PR-head question

The unprivileged validation job runs against pull-request-head content by
design — it checks the PR's own claims, and holds no credential. It therefore
also loads the PR's own `.github/index-policy.json`. That is not a
self-authorization hole:

- The policy path is outside every package root's refresh scope, so a pull
  request touching it is classified human-lane and can never auto-merge.
  Merging a widened policy requires a human, which is precisely the control.
- The credential is out of reach independently: the unprivileged lane builds
  its registry clients from an empty environment, so a PR-head policy can
  name any `base_url` and any `credentials_env` it likes and there is nothing
  for either to move. What it buys is a WARN saying its own claims went
  unverified.

The privileged subcommands deliberately do not read a local policy — they
never check the repository out, so they read the committed policy at the base
ref over the API instead.
