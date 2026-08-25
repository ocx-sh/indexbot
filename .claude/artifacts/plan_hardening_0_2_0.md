# Plan — `ocx-indexbot` 0.2.0 hardening: one job, one command, and the gate that holds

## Status

- **State:** done
- **Round:** 5 (hex-review security panel on `v0.2.0..v0.3.0`, 2026-08-25)
- **Shipped:** `v0.2.0` → `v0.2.1` → `v0.2.2` → `v0.3.0`, each release a defect
  the live deployments found rather than a plan item
- **Next:** none — see "What the live run found" below

## Where this came from

Two independent sources, both against `v0.1.0..main`:

1. A high-tier reviewer panel (spec, tests, quality, security, performance,
   docs, architecture, SOTA).
2. The live e2e on gitlab.com — four public projects, both governance lanes
   driven end to end against the real API. Findings prefixed `O-` were measured,
   not reasoned.

Every finding below cites a file:line that was read, or a request/response that
was made. Nothing here is speculative.

---

## WP-A — The gate (Block-tier; nothing ships until these hold)

### A-1 · Arming auto-merge is not bound to the head the gate judged
`cli/governance_poll.py:70` decides on `info.head_sha` (read at :66) and then
calls `enable_auto_merge(number)` — an iid, no SHA. Neither adapter passes the
parameter its forge provides for exactly this: `adapters/gitlab_api.py:217` PUTs
`{"merge_when_pipeline_succeeds": True}` (GitLab's merge endpoint takes `sha`,
which must equal source-branch HEAD or the call fails);
`adapters/github_api.py:170` sends `enablePullRequestAutoMerge` with only
`pullRequestId` (`expectedHeadOid` is available). The GitHub template's
*fallback* direct merge already gets this right —
`ci/templates/github/governance.yml` passes `--match-head-commit "$HEAD_SHA"` —
while the primary `--auto` arm three lines above does not.

Research narrowed the window: GitLab cancels `merge_when_pipeline_succeeds` when
new commits are pushed, and GitHub disables auto-merge when someone without
write permission pushes. So this is a TOCTOU at *arm time* rather than a
standing bypass — the arm can still bind a head the gate never evaluated, and
both forges hand us the guard for free.

**Do:** `ForgePort.enable_auto_merge(pr_number, *, head_sha)`. GitLab passes
`sha`, GitHub passes `expectedHeadOid`, the template passes
`--match-head-commit` on the primary arm too. A mismatch is not an error — it
means "re-classify next tick", so it must not abort the sweep.

### A-2 · The idempotency marker is squattable, and squatting it kills the lane
`adapters/gitlab_api.py:465` (`_find_marked_discussion`) returns the first
discussion whose first note merely *contains* `<!-- indexbot:governance -->` —
a public constant (`cli/governance_check.py:83`). No authorship check, no
`resolvable` check. A fork author posts a plain note containing that marker;
`create_comment` then finds it instead of opening the bot's own thread and
unconditionally `_put(…, {"resolved": False})` on a non-resolvable discussion →
4xx → `httpx.HTTPStatusError`, which is not an `IndexBotError` and therefore
escapes the sweep's per-MR guard (A-7). Effects: that MR never gets its blocking
thread — and on a fork MR the commit status does not gate, so nothing gates it
at all — and every later MR in the sweep goes untouched. It repeats every tick.

**Do:** resolve the token's own user id once (`GET /user`) and match only
`notes[0]["author"]["id"] == self_id and notes[0].get("resolvable")`. Same
authorship filter on `adapters/github_api.py:355`.

### A-3 · The GitLab reserved-namespace carve-out is a tautology
`ci/templates/gitlab/indexbot.yml:139`:
`if [ "$CI_MERGE_REQUEST_SOURCE_PROJECT_PATH" = "$CI_PROJECT_PATH" ]`. A fork
MR's pipeline runs **in the fork**, so `CI_PROJECT_PATH` *is* the source path
and the branch is taken for every merge request. `--allow-reserved-namespace` is
therefore passed unconditionally, and a fork can claim a segment the index
reserves for itself — i.e. publish under the operator's own brand. The GitHub
template compares against the target correctly.

**Do:** compare `$CI_MERGE_REQUEST_PROJECT_PATH` (the target project). Add a
named test over the rendered template; no existing test asserts the variable
choice.

### A-4 · `indexbot ci --check` cannot see a SHA → mutable-ref downgrade
`ci/render.py:157` `normalize_for_drift` strips the ref from *both* sides, so
`uses: actions/checkout@main` normalizes identically to `…@34e1148…`.
`scrape_pins` correctly refuses to *carry* a non-SHA ref, but the gate that runs
in CI never reports one. A pinned action in `governance.yml` — the
`pull_request_target` file whose `arm-auto-merge` job holds `contents: write` —
can be swapped for `@main` and `verify-indexbot-ci` stays green.

**Do:** blank the ref only when it `fullmatch`es a 40-hex commit sha; leave a
mutable ref verbatim so it drifts loudly.

### A-5 · A tag name is interpolated into a registry URL unencoded
`adapters/registry_v2.py:298` (and `:279`, `:318`, `:329`):
`f"{self.base_url}/v2/{repo_path}/manifests/{reference}"`. `reference` is a tag
KEY from a PR-authored root, and nothing in the bot validates a tag name — the
grammar lives only in the served `schema/root.schema.json`, which no generated
pipeline runs over a PR's diff. httpx collapses `..` client-side, so a tag of
`../blobs/sha256:<hex>` retargets the request to a blob in the same repository
and `observe_one_tag`'s "must be an image index" check then passes against
attacker-pushed bytes that were never a tagged manifest.
`check_digest_in_scope`'s documented invariant is defeated by construction.

**Do:** `quote()` `reference` and `repo_path` in all four builders, and enforce
the wire grammar in the bot: `re.fullmatch` the schema's tag pattern in
`parse_package_root`.

### A-6 · A GitLab commit status is a state machine, and `pending → pending` 400s
Measured live, 2026-08-25:

    POST /projects/85719427/statuses/<sha> {"state":"pending",…}  → 201
    POST … the same payload again                                 → 400
      {"message":"Cannot transition status via :enqueue from :pending …"}
    POST … {"state":"success"} twice                              → 201, 201

`pending` is the human lane's steady state, so **the second poll tick of any
merge request awaiting review crashes the poller** — observed:
`governance_poll.py:67 → governance_check.py:247 → gitlab_api.py:278`.

**Do:** `set_commit_status` treats "already in the requested state" as a no-op —
catch the 400, re-read `/repository/commits/<sha>/statuses`, return quietly when
the context already holds that state, re-raise otherwise.

### A-7 · The sweep's per-MR isolation catches only `IndexBotError`
`cli/governance_poll.py:85`. The module docstring promises in bold that one MR's
failure never ends the sweep, and the whole GitLab lane's fail-closed argument
rests on the poller reaching every MR. But `httpx.HTTPStatusError`, `KeyError`
(a PR closed between listing and fetching), `LookupError`, `GraphQLError` and
`json.JSONDecodeError` all escape. A-6 escaped this way in production.

**Do:** wrap adapter HTTP failures into `IndexBotError` subclasses at the adapter
boundary (root cause), and widen the loop to `except Exception` mapped to
`ExitCode.TRANSIENT` (belt). Keep the failure's identity in the stderr line.

---

## WP-B — One job, one command

The standing requirement: every job on both forges is a single `indexbot`
invocation, and the docs explain the pieces well enough to write your own CI.
The architecture reviewer argued against collapsing `validate` and `reconcile`
(a `git` dependency; CI-presentation syntax in the domain layer). Both concerns
are real and both are answerable, and the requirement stands:

- `git` is already a hard dependency of both generated pipelines (the checkout,
  and `uvx --from git+…`), and `subprocess` is stdlib, so BD-1 holds. What moves
  into the bot is the knowledge currently living as a thirty-line comment
  duplicated across two templates — the `:(glob)` pathspec and the three-dot
  rule — which is exactly the kind of thing that belongs in one tested place.
- Annotation syntax is presentation, but the bot already owns presentation:
  `cli/_common.write_ci_summary` writes GitHub step summaries today. Emitting the
  matching `::error`/`::warning` line is the same layer, not a new one.

Forge vocabulary stays in the template: each command takes the base SHA and the
provenance flag as arguments, so the bot never learns
`$CI_MERGE_REQUEST_DIFF_BASE_SHA` vs `github.event.pull_request.base.sha`.

| # | Job | Today | After |
|---|---|---|---|
| B-1 | `governance-gate` | `classify-pr` then `governance-check`, two processes, each re-fetching the PR and re-deriving the classification; then a hand-rolled `gh pr merge` arm step and a withdraw step | `indexbot governance-gate --pr N` — one fetch, classify, label, gate, arm or withdraw. `governance_poll._gate_one` already has this shape; both lanes call the same function |
| B-2 | `validate` (both forges) | 3 steps: changed-file pathspec, base-ref materialization, `indexbot validate` | `indexbot validate-pr --base-sha <sha> [--same-repo]` |
| B-3 | `reconcile` (both forges) | 1 command + 3 exit-code translation steps | `indexbot reconcile --anomaly-ok`, the bot writing its own annotation |
| B-4 | `pr-checks-label` | a `gh api` + two `jq` filters + `gh api` | `indexbot label-failed-run --head-sha <sha>` — over `ForgePort`, so it exists on both forges |
| B-5 | `stale` | `actions/stale`; **no GitLab counterpart at all** | `indexbot stale` — ADR-6 FP-8's second half, both forges |
| B-6 | GitLab pipeline | three jobs | plus the FP-8 lane B-4/B-5 give it |

B-1 also closes a dead-code gap: `ForgePort.enable_auto_merge` has **zero**
production callers on GitHub today, because `arm-auto-merge` bypasses the port
entirely. After B-1 the port is the only arm path on both forges, which is what
makes A-1's fix actually cover GitHub.

---

## WP-C — Documentation: make writing your own CI possible

- **C-1 `docs/reference/architecture.md` (new).** The interconnection document —
  the single largest gap. Per forge, one row per job: trigger → subcommand →
  what it reads (checkout? API? which ref?) → token and scope → what it writes →
  **who consumes that output**. It must state that `classify-pr`'s
  `classification` output is written and read by nobody: `governance-check`
  re-derives the classification itself. An operator building this wiring from
  today's docs would get that wrong.
- **C-2 `docs/reference/gitlab.md` (new).** Everything currently trapped in the
  GitLab template's header comment: the two required project settings, the two
  schedules and their exact `INDEXBOT_LANE` values (undocumented anywhere in
  `docs/`), the `api` token scope, and the thread-versus-status gate mechanics.
- **C-3 `docs/reference/cli.md`.** Says "eight subcommands"; there are eleven,
  becoming more with WP-B. Every subcommand, every flag, every environment
  variable, per-command required/optional.
- **C-4 The `--tags` callout.** `--tags` REPLACES the curated set. Measured:
  announcing `1.1.0` deleted `1.0.0` from the root. The natural CI shape the
  docs invite — `--tags $CI_COMMIT_TAG` on tag push — silently withdraws every
  previously announced tag, and the obvious workaround (`git tag --list`)
  truncates under a shallow clone. Loud callout in `cli.md` and `quickstart.md`,
  and the generated examples must use `--tags-file`.
- **C-5 `docs/guide/quickstart.md`.** Its policy example is now invalid (`name`
  and `name_segments` are required); it pins `==0.1.0`; it never mentions
  `indexbot ci`, and never mentions GitLab.
- **C-6 Schema drift.** `schema/index-policy-v1.schema.json` has
  `additionalProperties: false` on `ci` and omits `deploy_workflow`, which
  `core/policy.py:322` accepts and `docs/reference/policy.md:40` documents. The
  corpus test that exists to catch exactly this has no fixture exercising it.
- **C-7** `README.md` and `docs/index.md` carry duplicate subcommand tables,
  already independently stale. One source.
- **C-8** A curated `CHANGELOG.md` for 0.2.0.
- **C-9** Two operator-facing constraints found live, currently undocumented:
  `registry.gitlab.com` rejects the OCX package media type
  (`400 MANIFEST_INVALID: unknown media type:
  application/vnd.sh.ocx.package.v1`), so an index may be hosted anywhere but its
  packages cannot live in a media-type-filtering registry; and orphan CAS objects
  on a reused announce branch are deliberate — every committed CAS file is
  hash-verified and must parse as an image index, and pruning would break a
  client holding a lock on an older digest.

---

## WP-D — Correctness, quality and cost

| # | Finding | Fix |
|---|---|---|
| D-1 | `render --index-dir .` silently renders an **empty** index — no roots, `{"packages":{}}`, exit 0. `cli/render.py:60` builds prefix `./p/`, which matches nothing. Reproduced live. | Normalize `.`/`./` to `""`, **and** make a plan that discovers zero roots a hard error unless `--allow-empty`. The silent-empty path is the dangerous half |
| D-2 | `--tags-file` has no comment syntax; a `#` line fails with "check for a typo" (`cli/announce.py:145`) | Skip blank lines and `#` comments |
| D-3 | `governance-poll` prints nothing per MR | One line per MR: number, classification, disposition |
| D-4 | The governance comment says only "human-review-required: awaiting human review" — never *why*. Measured on the owners-gate scenario | Carry the disposition's reason: which root, which check, what resolves it |
| D-5 | `list_approvals` discards `head_sha` on GitLab (`gitlab_api.py:338`), and an approval outranks every other disposition including `auto_merge: never` | Read `reset_approvals_on_push` from the project and fail closed when it is off, rather than documenting the requirement |
| D-6 | `Link: rel="next"` is followed to any host with the write-scoped token attached (`adapters/_http.py:85`). `registry_v2._parse_next_link` already does this correctly | Reject a `next` whose netloc differs from the initial URL's |
| D-7 | `ci.*` policy values are unvalidated free strings substituted into privileged YAML (`ci/render.py:195`, `core/policy.py:302`) | Reject `\n`/`\r`; `fullmatch` the crons |
| D-8 | The dotenv sink denylists two characters (`cli/_common.py:97`) | Allowlist `^[A-Za-z0-9_.:/-]+$` |
| D-9 | `for file in $changed` unquoted in the GitLab template | `IFS`/`set -f`, or a here-doc-fed `while read` |
| D-10 | `change_class: str` in `governance_check.py:130,226` discards `ChangeClass`'s exhaustiveness | Type both signatures `ChangeClass` |
| D-11 | `AutoMerge`/`Forge` members listed in three places (`core/policy.py:70,86`, `cli/announce.py:93`) | `typing.get_args` as the single source |
| D-12 | The ownership recheck re-fetches a root `classify_pull_request` just parsed (`governance_check.py:117` vs `classify_pr.py:141`); `maintainers.yml` is fetched twice on the human path (`:205`, `:216`) | Thread the parsed roots and the reviewer list through |
| D-13 | `GitLabApi` opens a fresh `httpx.Client` per call — `commit_files` costs N+2 TLS handshakes. `GitHubApi` shares one client per logical operation | Match GitHub's shape |
| D-14 | No test asserts `resolve_review_thread` fires from `gate_pull_request`'s success branch — delete the call and every test passes | Assert the marker is released on success and held on pending |
| D-15 | `schema` is the only DISPATCH entry with no through-`main` test | Add one |
| D-16 | Stale docstrings: `cli/render.py:88` ("exactly two path segments"), `cli/validate.py:1-25` (a binding convention that never shipped) | Rewrite |

---

## Verification

1. `task verify` in `ocx-indexbot` — ruff format, ruff, pyright strict, pytest at
   `fail_under = 100` branch. Every fix above lands with a **named** test that
   fails if the fix is reverted.
2. `indexbot ci --check` against `ocx-sh/index` still reproduces the reviewed
   workflows byte-for-byte, and `task bot:test` / `task bot:workflows` stay green.
3. The e2e re-runs against the final implementation: both governance lanes, the
   owners-gate negative, the Pages-served wire tree, and `ocx` resolving
   `e2e.ocx.sh/e2e/app` through it.
4. A-2, A-3 and A-6 each get an assertion at the level they broke — an adapter
   test for the marker authorship and the status transition, a rendered-template
   test for the provenance variable.

---

## Parallelization

Repo policy: commit straight to `main`, no PRs. Work packages run in ephemeral
worktrees under `.agents/worktrees/` and merge back to `main` in topological
order, `task verify` after every merge.

| WP | Scope | Expected files | Size | Wave | Depends on | Review | Status |
|---|---|---|---|---|---|---|---|
| WP1 | A-4 drift gate sees a mutable ref; D-7 `ci.*` values validated | `src/ocx_indexbot/ci/render.py`, `src/ocx_indexbot/core/policy.py`, `tests/ci/test_render.py`, `tests/core/test_policy.py` | M | 1 | — | panel | pending |
| WP2 | A-5 URL-encode the registry reference + enforce the tag grammar in the bot | `src/ocx_indexbot/adapters/registry_v2.py`, `src/ocx_indexbot/core/validate_entry.py`, `tests/test_registry_v2.py`, `tests/test_validate_entry.py` | M | 1 | — | panel | pending |
| WP3 | A-3 the GitLab provenance variable; D-9 the unquoted changed-file loop | `src/ocx_indexbot/ci/templates/gitlab/indexbot.yml`, `tests/ci/test_templates.py` | S | 1 | — | panel | pending |
| WP4 | A-1 head-SHA pin, A-2 marker authorship, A-6 the status state machine, A-7 sweep isolation, D-5 approval freshness, D-6 pagination host, D-13 client reuse | `src/ocx_indexbot/ports.py`, `adapters/{github_api,gitlab_api,_http}.py`, `cli/{governance_poll,governance_check}.py`, `errors.py`, their tests | L | 1 | — | panel | pending |
| WP5 | B-1..B-6 one job one command: `governance-gate`, `validate-pr`, `reconcile --anomaly-ok`, `label-failed-run`, `stale`, and both template sets | `cli/*.py` (new subcommands), `cli/{_wiring,main}.py`, `ci/templates/**`, their tests | L | 2 | WP3, WP4 | panel | pending |
| WP6 | C-1..C-9 documentation, schema drift, changelog | `docs/**`, `README.md`, `src/ocx_indexbot/schema/index-policy-v1.schema.json`, `tests/fixtures/policy/accept/*`, `CHANGELOG.md` | L | 3 | WP5 | panel | pending |
| WP7 | D-1..D-4, D-8, D-10..D-12, D-14..D-16 correctness, typing, cost, missing wiring tests | `cli/{render,announce,_common,governance_check,classify_pr}.py`, `core/policy.py`, `tests/**` | M | 3 | WP5 | panel | pending |

WP1–WP4 are file-disjoint and launch together. WP5 rewrites the command surface
WP6 documents, so the docs wave follows it rather than racing it.


## What the live run found (2026-08-25)

Four releases in one day, because running the thing is a different test from
reading it. Every one of these was found by a real deployment, not by review:

| Release | Defect | Found by |
|---|---|---|
| `0.2.1` | The GitLab governance poll ended on a 400 re-posting a commit status it had already posted — the listing endpoint it consulted is not scoped by the ref the POST carried | The scheduled poll on `michael-herwig/e2e-indexbot-index`, MR !6 |
| `0.2.1` | `validate-pr` refused any pull request whose `.github/index-policy.json` differed from the base ref's, inverting the control that keeps the file committed rather than a settings-page value | `ocx-sh/index#735` — the switchover PR failed its own gate |
| `0.2.2` | The base-ref policy read refused a base ref carrying no *readable* policy, so the v1→v2 migration PR refused itself and left direct-push-to-default as the only route | `ocx-sh/index#735` again, one fix later |
| `0.3.0` | A pull request reclassified between sweeps kept its previous lane label; MR !6 merged as `refresh` still carrying `human-review-required` | The live GitLab lane |
| `0.3.0` | `workflows-check` audited GitHub only, on a release whose thesis is forge parity | Noticed while auditing the e2e index by hand |
| — | `oven/bun:1-alpine` unpinned in the e2e index's own root pipeline | GL-01, the first time it ran |

### Scenarios proven live

Against `michael-herwig/e2e-indexbot-{app,index,fork}` on gitlab.com:

- Announce from a tag pipeline → fork commit over the API → merge request
  against the parent, on a **spent** fork branch whose previous merge request
  had already merged. Handled: the branch was rebuilt from the index's current
  main, not stacked on the stale tip.
- Registry `registry.gitlab.com`, prefix `e2e.ocx.sh` — neither one anything an
  OCX deployment allowlists or serves.
- The fork merge request's pipeline, then the parent's **scheduled** poll
  classifying it `refresh`, posting the external commit status, arming
  auto-merge, and the merge landing.
- Pages serving a valid wire tree: `/config.json`, `/c/index.json`,
  `/p/e2e/app.json` with all three announced tags.
- **Q3, deferred from WP-0 and measured here** — with a stronger result than
  the claim: see `research_gitlab_gate.md`. A protected parent variable is
  absent from a merge-request pipeline *even when GitLab runs that pipeline in
  the parent project*, because protection is a property of the ref.
