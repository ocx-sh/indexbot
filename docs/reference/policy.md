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
| `registry_hosts` | yes | — | Hosts whose `oci://` repositories this index admits |
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
choosing — and a publisher running `announce` from a laptop is on neither
runner regardless.

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

## Registries this bot can serve

An allowlisted host must also have a registry client wired for it, or its
roots would validate and then fail every byte fetch. Today that set is:

| Host | Token endpoint | Notes |
|---|---|---|
| `ghcr.io` | `https://ghcr.io/token` | Registry v2 default |
| `ocx.sh` | Artifactory's own path | not `https://ocx.sh/token`, which 404s |
| `registry.gitlab.com` | `https://gitlab.com/jwt/auth` | token `service` is the literal `container_registry`, **not** the host |

Allowlisting anything else is refused at startup, naming what is missing.

## Why the host shape is strict

The allowlist is matched against a URL's parsed `hostname`, which is always
lowercased and never carries a port. So `https://harbor.corp`, `Harbor.Corp`
and `harbor.corp:5000` would each parse happily as policy entries and then
match nothing at all — an allowlist that silently admits no one. Rejecting
them at parse time is the difference between a loud error and an index where
every download fails for a reason nobody can see.

A registry on a non-standard port is allowlisted by its bare host:
`harbor.corp` admits `oci://harbor.corp:5000/team/tool`.

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

## Two failures, both early and loud

**No policy file — fail closed.** An index copy that never stated a policy
says so, rather than inheriting the public index's hosts by accident.

**A host no adapter can serve.** The bot fetches from the hosts it has a
registry client wired for, and nothing else. Allowlisting
`harbor.corp.internal` today would produce roots that pass every validation
check and then cannot be fetched — strictly worse than the honest refusal it
replaces. The error names the missing piece: implement the client, add its
host, dispatch it, in the same change.

## The PR-head question

The unprivileged validation job runs against pull-request-head content by
design — it checks the PR's own claims, and holds no credential. It therefore
also loads the PR's own `.github/index-policy.json`. That is not a
self-authorization hole:

- The policy path is outside every package root's refresh scope, so a pull
  request touching it is classified human-lane and can never auto-merge.
  Merging a widened policy requires a human, which is precisely the control.
- The no-adapter guard closes it independently: a PR-head policy naming a host
  with no client fails the run outright.

`announce` is the one flow that deliberately does not read a local policy —
its publisher runs outside any index checkout, so it reads the target index's
committed policy at the base ref over the API instead.
