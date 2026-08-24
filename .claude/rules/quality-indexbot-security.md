# Index Bot Security Guardrail

Always-loaded (no `paths:`) — this is the highest-risk surface in the repo (the
`indexbot` write path processes untrusted fork-PR data under privileged CI). The
bars below fire during planning and before any `src/ocx_indexbot/**`
edit, not just when a file happens to auto-load. Design authority lives in the
reference deployment, [ocx-sh/index](https://github.com/ocx-sh/index), under
`.claude/artifacts/`: ADR-6 (`adr_fork_pr_announce.md`, FP-1/FP-4/FP-5/FP-7)
and ADR-4 (`adr_index_bot_and_workflow_security.md`, BD-3/BD-4/BD-5 +
Amendment A1), plus `docs/reference/contracts.md` §12/§14 here.

**The workflow invariants below describe an index repository's workflows, not
this package's own.** They are enforced from here by `indexbot workflows
check`, which an index repo runs against its `.github/workflows/`.

## Security bar (Block-tier — never negotiate)

- **One trigger per workflow file.** `pull_request` and `pull_request_target`
  fire on the same PR head commit, so a workflow declaring both must pick a
  half with a job-level `if: github.event_name == ...` — and a job skipped by
  such an `if:` STILL emits a check run, conclusion `skipped`, under its own
  name. GitHub counts `skipped` as satisfying a required status check and
  resolves duplicate-named contexts to the most recent, so the privileged run
  publishes a green-equivalent impostor of the unprivileged half's required
  context. That is why `schema-validate-pr` (`validate.yml`, `pull_request`)
  and `governance-gate`/`arm-auto-merge` (`governance.yml`,
  `pull_request_target`) live in separate files. Never merge them back, and
  never re-add a `github.event_name` guard to either.
- **Untrusted-PR-data-only contract.** The privileged governance job
  (`pull_request_target`, `governance-gate` in `governance.yml`) NEVER checks out
  or executes PR-head content. It acts through the GitHub API and base-branch
  data only. PR-head code runs solely in the zero-secret `pull_request` job
  (verify-claims). Any workflow edit that adds a PR-head checkout (`ref:` at
  `pull_request.head`) to a credentialed job breaks the entire safety argument
  (FP-7, G-16). The security suite asserts absence of ANY `ref:` in that job.
- **Authorization is `owners[].github_id` from the BASE ref only.** A PR author
  is an owner iff their numeric `github_id` is in the *committed* root's
  `owners[]` on the base branch (FP-5, G-19). Never read authorization from
  PR-head content — a PR editing its own `owners[]` is itself a G-05 human-lane
  change, and must not self-authorize. Bind on `github_id` (rename- and
  login-recycling-proof), never `login`.
- **SSRF/host-allowlist runs before the first registry call.** `repository`
  hosts arriving in root data are allowlist-checked (`check_repository_allowlisted`)
  before any `RegistryPort` request (BD-1 ordering, G-03). Keep the guard first;
  the ordering has a dedicated test.
- **The allowlist is a committed file, never a variable.** G-03's host set is
  per-deployment policy (`.github/index-policy.json`, `core/policy.py`), loaded
  by `cli/_wiring.py` and passed in as a required `allowed_hosts` argument.
  Never move it to an env var, `vars.` or `secrets.` — "extend only via reviewed
  PR" is the control, and a settings-page value widens registry trust with no
  diff and no reviewer. Never give `check_repository_allowlisted` a default
  either. A host with no `RegistryPort` adapter is refused at wiring time
  (`_wiring.REGISTRY_ADAPTER_HOSTS`): allowlisting what cannot be fetched
  produces roots that validate and then fail every download. This repo's own
  policy stays exactly `{"ghcr.io", "ocx.sh"}` — third-party mirrors and the
  operator's own first-party repositories, both served by
  `adapters/registry_v2.py` — pinned by a named test in
  `tests/security/test_governance_contracts.py`. Widening it further needs a
  client wired in `_wiring._registry()` first, in the same PR.
- **ND-4 gates claiming, not updating — and the exemption is doubly scoped.**
  A reserved segment exists so a stranger cannot *claim* `p/ocx/**`. An
  announce that only moves `tags` on a root already committed on the base ref
  is not a claim, so `cli/validate.py` retracts the rejection — but only when
  `core/diff.classify_change(base, head) == "refresh"` (never a hand-rolled
  second field list) AND only for `RESERVED_BRAND_SEGMENTS`. Both halves are
  load-bearing: without the first, a fork could repoint an existing
  first-party root's `repository` behind a green REQUIRED check; without the
  second, base-ref state would unlock control-path segments (`p`, `o`) whose
  collision is with the URL layout itself. No `--base-dir`, or no such root at
  the base ref, is always "fresh claim".
- **Digest `re.fullmatch` before any path join.** A `sha256:[a-f0-9]{64}` value
  is `fullmatch`-validated before it is used to build a CAS path; `LocalFiles`
  rejects `..` and absolute paths before touching the filesystem.

## Coverage bar (do not lower)

- `fail_under = 100` branch coverage is a design constraint (ADR-4 BD-3), NOT a
  target — never lower it (an earlier 90% suggestion was owner-corrected back to
  100). `task test` == CI's gate.
- No inline `# pragma: no cover`. The only exclusion is the reviewed
  `exclude_also` list in `pyproject.toml` (main-guard, `TYPE_CHECKING`, `...`).
- New branches (short-circuits, env-absent paths) get explicit covering tests in
  the same change — the gate fails immediately otherwise.

## Per-contract test bar

- Every governance contract G-01..G-20 and every threat class carries a named
  test in `tests/security/` (`test_governance_contracts.py` 1:1 with the
  contracts; `test_threat_classes.py`; `test_workflow_split.py` for the static
  workflow invariants). This enumerated suite is the audit artifact — it is
  deliberate DAMP duplication of invariants also covered by unit tests, not
  incidental.
- Adding or changing a governance rule (a G-05 key, a lane decision, an anomaly
  predicate) requires updating its named security test in the same change.
- Retired contracts (G-08/G-17/G-18 under the fork-PR lane) keep ABSENCE tests —
  proof the retired surface (repository_dispatch payload reader, announce PAT,
  reconcile `--dry-run`) stays gone.

## Untrusted-input hygiene (BD-4, carried forward)

- Length-cap → `re.fullmatch` (never `match`/`search`) → reject `..`/absolute
  paths. No nested quantifiers (`re` has no timeout — ReDoS).
- In workflows, pass untrusted values via env-var indirection, never `run:`
  interpolation. Untrusted PR content that reaches a step summary goes inside a
  fenced block, never into a `::error` title or any shell-evaluated position.

## Byte-exact serializer (CONTRACTS §14)

The canonical serializer is a hand-written, spec'd writer (root: 2-space indent,
insertion-order fields, trailing newline; CAS: minified, alphabetized,
`ensure_ascii`, no trailing newline). Never "fix" it with a generic
pretty-printer — ambient JSON key ordering is a correctness bug here, and the
golden fixtures (`tests/golden/serializer/`) gate against exactly this.
