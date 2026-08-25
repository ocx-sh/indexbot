# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.2] - 2026-08-25

### Fixed

- Let the pull request that adopts or repairs the policy through *(validate-pr)*

## [0.2.1] - 2026-08-25

### Fixed

- Read the refusal, not a second endpoint, to recognise a no-op status write *(gitlab)*
- Read the deployment policy from the base ref, do not refuse the pull request *(validate-pr)*

## [0.2.0] - 2026-08-25

### Added

- The deployment declares its own identity *(policy)* **BREAKING**
- A second registry host, and proof of what it served *(registry)*
- A second forge — GitLab REST v4 *(forge)*
- A poll lane for GitLab, and an auto-merge dial *(governance)*
- One job, one command *(cli)*
- Generate an index's pipeline, on either forge *(ci)*

### Documentation

- The 0.2.0 surface, and how to build a pipeline on it

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
[0.2.2]: https://github.com/ocx-sh/ocx-sdk-python/compare/v0.2.1..v0.2.2
[0.2.1]: https://github.com/ocx-sh/ocx-sdk-python/compare/v0.2.0..v0.2.1
[0.2.0]: https://github.com/ocx-sh/ocx-sdk-python/compare/v0.1.0..v0.2.0
[0.1.0]: https://github.com/ocx-sh/ocx-sdk-python/tree/v0.1.0

