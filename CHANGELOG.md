# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-08-25

Any index, any forge. 0.1.0 was the extraction of the bot that runs *the*
OCX public index on GitHub; 0.2.0 is the release in which a stranger can
stand up their own index, with their own name, their own registry and their
own merge policy, on GitHub or GitLab — proven end to end against four
public gitlab.com projects and the real `ocx` client.

### Added

- **`.github/index-policy.json` v2** — an index declares its own `name` and
  `name_segments`, both required with no default. `reserved_namespaces`,
  `governance.auto_merge` and a `ci` block join the registry-host allowlist.
  `indexbot schema` prints the shipped JSON Schema, and a shared fixture
  corpus keeps parser and schema agreeing.
- **GitLab as a first-class forge** — `ForgePort` (was `GitHubPort`) with a
  GitLab REST adapter beside the GitHub one, `announce --forge gitlab`, and
  `registry.gitlab.com` support.
- **`indexbot governance-poll`** — the whole governance lane as a scheduled
  sweep, for a forge with no privileged pull-request trigger. One merge
  request's failure never ends it.
- **`indexbot ci`** — generates this index's pipeline files from its
  committed policy, for either forge, with `--check` as the drift gate.
- **One job, one command.** `validate-pr`, `governance-gate`,
  `label-failed-run` and `stale` fold what were multi-step shell jobs — a
  `git` pathspec, base-ref materialization, `jq` filters, exit-code
  translation, a third-party stale action — into single commands, tested in
  Python and identical on both forges. `reconcile --anomaly-ok` completes
  the set. See [Build your own pipeline](docs/guide/ci.md).
- N-segment package names throughout: `PackageId` carries segments, not a
  namespace and a package.

### Fixed

- **`ci.run` no longer defaults to `uvx ocx-indexbot`.** That default rendered
  into `arm-auto-merge`, a job holding `contents: write` under
  `pull_request_target`, and `uvx` resolves the latest release when the step
  starts — so an operator who committed the minimal documented policy got a
  privileged job running whatever PyPI held that morning, with every gate in
  this package reporting the pipeline clean. `ci.run` is now required for
  `indexbot ci` and refused when it resolves at job start; the new **WF-08**
  audits the same property over a rendered or hand-written tree.
  `uv run --project <dir> -- indexbot` is refused with it: without `--frozen`
  or `--locked`, `uv run` re-locks a stale lockfile and a git source moves.
  WF-06's docs claimed WF-02 covered this — WF-02 inspects `uses:` refs and
  never saw a `run:` command's package resolution.
- **A digest reference is a demand.** `get_manifest` cross-checked only the
  optional `Docker-Content-Digest` header; with it absent nothing compared
  what was asked for against what came back.
- **Blob redirects.** ghcr.io answers a blob GET with a 307 to a CDN. httpx
  does not follow redirects and a 3xx is not an error status, so `get_blob`
  returned the redirect body as the blob — every desc blob read from ghcr.io
  was wrong bytes. Redirects are followed now, with the registry token
  dropped on any hop that leaves the origin.
- **Desc layers are verified** against the digest their manifest declared,
  rather than stored addressed by their own hash — which is self-consistent
  whatever came back.
- **Auto-merge is bound to the revision that was gated** (`expectedHeadOid`
  on GitHub, `sha` on GitLab). A head that moved is a race, not a failure.
- **A GitLab approval counts only for the revision it was granted to.** The
  documented remedy is Premium, so freshness comes from two server-generated
  timestamps instead — the newest diff version's and the `approved` event's.
- **The governance marker is public**, so a pull request's author could plant
  it. GitLab matches only its own *resolvable* thread — a plain note carrying
  the marker disarmed the discussion gate outright — and GitHub only a
  comment from the repository side.
- **The blocking artifact goes up before the status and comes down after
  it.** A GitLab status has no transition out of `success`, so the failed
  write used to abort the run before the thread was re-opened.
- **One merge request's failure cannot end a governance sweep.** A raw
  `httpx.HTTPStatusError` did exactly that in production; adapters wrap their
  own failures now and the sweep guards against anything else.
- **A GitLab commit status is a state machine** — re-posting the state it
  already holds is a 400, and `pending` is the human lane's steady state.
- Registry URLs percent-encode every interpolated component, and tag keys are
  checked against the wire grammar at the boundary — a tag named
  `../blobs/sha256:<hex>` retargeted a manifest fetch at a blob.
- Pagination refuses a `Link: rel="next"` that leaves the API host.
- The rendered-workflow drift gate no longer normalizes away a *mutable*
  action ref, which let a pinned SHA be hand-edited to `@main` and stay green.
- The GitLab reserved-namespace carve-out compared a fork against itself
  (`CI_PROJECT_PATH` is the fork's own path in a fork pipeline), granting
  `--allow-reserved-namespace` to every merge request.
- `render --index-dir .` rendered a valid, empty index over a populated one;
  `.`/`./` normalize, and a render that finds no roots is refused unless
  `--allow-empty` says a new index is meant.
- `--tags-file` skips `#` comments instead of failing with "check for a typo".
- The GitLab dotenv sink allowlists its values rather than denylisting two
  characters.

## [0.1.0] - 2026-08-24

### Added

- Phase 1 contracts freeze — schemas, indexbot scaffold, CI gates, taskfile (#12)
- Phase 2 bot — indexbot core, adapters, CLI, announce/reconcile/validate workflows (#14)
- Sparse enumeration index /c/index.json + superseded_by root field (#20)
- Catalog.json view-model, retire wrapper-page emission (#21)
- Fork-PR announce bot core — verify-claims, curated announce, verify-only reconcile (phase 2) (#48)
- Announce-lane hardening + fail-closed machine-lane path allowlist (Track B) (#51) *(bot)*
- Per-deployment registry-host allowlist as a committed file (#56) *(policy)*
- Record the build source repository on a package root (#58) *(schema,bot,site)*
- The index stores OCI image indices, verbatim (#62) *(bot,schema,site)* **BREAKING**
- Claim the three namespaced ocx.sh roots, and serve ocx.sh (#70)
- Publish the name grammar in config.json (#75) *(schema,bot)*
- ND-4 gates claiming a reserved segment, not updating one (#77) *(validate)*
- Record the observed variant set on the package root (#101) *(root)*
- UX review overhaul *(site)*
- Render the catalog site from @ocx-sh/catalog (#719)
- Import indexbot from ocx-sh/index and rename to ocx_indexbot
- Add indexbot workflows-check (WP-3)

### Changed

- Derive catalog variants from tags instead of the root field (#110) *(render)*
- Stop recording variants, the second writer must agree (#114) *(regenerate)*

### Documentation

- Site-redesign documentation + governance trail (#27)
- Document the served c/index.json envelope and config key set (#90) *(contracts)*
- Write the 0.1.0 documentation set (WP-4)

### Fixed

- Registry-reality — ghcr 403 handling, real mirror.yml shape, brand-namespace mechanism (#17)
- Bot/ci/docs review round 1 — href scheme gate, smoke masking, Z-anchored timestamps (#29)
- Add feature fields to platform sort key (#45)
- Scope the validate changed-files pathspec to package roots (#59) *(ci)*
- Arm auto-merge from a checkout-free job, and disarm on the human lane (#63) *(ci)*
- Stop pull_request_target emitting a skipped schema-validate-pr (#76) *(ci)*
- Dispatch render-deploy from the run that armed the merge (#67) (#84) *(ci)*
- Make the deploy-on-merge poll fail-open and document the run flip (#89) *(ci)*
- Never write an empty title (#93) *(desc)*
- Check build-pinned tags, exempt rolling cascade targets (#105) *(anomaly)*
- Accept an absent variants field, keep the tamper check (#112) *(validate)*
- Retry registry transport failures instead of crashing (#278) *(bot)*
- Merge directly when auto-merge arm finds checks already green *(ci)*
- Validate only the roots a PR authored (three-dot diff) *(ci)*
- Base fresh announce branches on upstream main, not fork main (#593) *(bot)*
- Bump locked pip to 26.2.1 for PYSEC-2026-3721 (#718)
- Apply index-side review findings (round 1) (#720)
- Wheel:check failed because its search succeeded *(ci)*
[0.2.0]: https://github.com/ocx-sh/indexbot/tree/v0.2.0
[0.1.0]: https://github.com/ocx-sh/indexbot/tree/v0.1.0

