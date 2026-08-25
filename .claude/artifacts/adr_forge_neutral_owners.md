# ADR — Forge-neutral `owners[]`, and one announce implementation

- **Status:** Accepted
- **Date:** 2026-08-25
- **Lands in:** `ocx-indexbot` 0.5.0
- **Supersedes (partially):** ADR-1 D2's field names, ADR-6's
  `indexbot announce` reference implementation
- **One-way door:** yes for the wire field names — `p/<ns>/<pkg>.json` is a
  frozen URL shape whose *field semantics* are equally frozen (see
  `docs/reference/contracts.md` §5.6). This ADR is why the migration is
  additive rather than a rename in place.

## Context

Two defects surfaced once the bot became decentralised (0.2.0 — any index,
any forge) and was proven on gitlab.com:

1. **`owners[].github` / `owners[].github_id` name a forge that may not be
   GitHub.** A gitlab.com index writes its own maintainer as
   `{"github": "michael-herwig", "github_id": 2223282}` where the value is a
   *GitLab* login and a *GitLab* numeric user id. The field names are now
   actively misleading — the same root read by a human implies a GitHub
   account that does not exist. The same is true of
   `.github/maintainers.yml`'s entries, which reuse `model.Owner`.

2. **The bot ships a second implementation of the wire-format writer.**
   `indexbot announce` (Python) and `ocx package announce` (Rust,
   `ocx-sh/ocx`) both build a package root plus its CAS objects from live
   registry truth and open a fork PR. Nothing in production calls the Python
   one: `ocx-mirror`'s pipeline shells out to `ocx package announce`
   (`src/pipeline/ocx_cli/announce.rs`), from four of its commands. Two
   implementations of one byte-exact format, in two languages, with only one
   on a real path, is a divergence generator — and the untested half is the
   one this project's own e2e suite was exercising, which meant the e2e was
   proving the wrong writer.

`owners[]` itself is **not** dead and must not be treated as cosmetic. It is
the G-19 ownership key: `cli/governance_check.py` matches the pull request
author's numeric forge id against every touched root's `owners[]` to decide
whether the machine (auto-merge) lane may run at all. `ocx` and `ocx-mirror`
never *claim* ownership — they only publish — which is why both pass the
field through untouched and neither parses its shape. That asymmetry is what
makes the rename cheap, not evidence that the field is unused.

## Decision

### D1 — `login` and `id`, forge-neutral

`owners[].github` → `owners[].login`; `owners[].github_id` → `owners[].id`.
Same rename for `.github/maintainers.yml` entries. `model.Owner` becomes
`Owner(login: str, id: int)`.

`login` is a **username** — the handle a forge API resolves to a user id —
and never a display name. `ForgePort.request_reviewers` takes exactly this
string and, on GitLab, resolves it through `GET /users?username=<login>`;
GitLab's `name` is a different field (the human's full name) and using it
would silently request review from nobody. The published schema description
says so, because the shorter name makes the wrong reading easier.

### D2 — read both, emit both, refuse disagreement

- **Read:** `login` wins; fall back to `github`. Same for `id`/`github_id`.
  A root carrying only the legacy pair parses unchanged — that is what makes
  the migration a non-event for every already-published index.
- **Emit:** both pairs, with `github`/`github_id` **derived** from
  `login`/`id` at serialization time. They are not independently settable.
- **Refuse:** `parse_package_root` raises `ValidationError` when a root
  carries both spellings and they disagree. Derivation on the write side plus
  refusal on the read side is what makes drift unrepresentable; without the
  refusal, a hand-edited root could carry one identity for a human reader and
  another for the auto-merge gate — a privilege-relevant divergence.

No `format_version` bump: emitting a strictly larger object is additive, and
every existing reader (the ocx client parses `IndexRoot` without an `owners`
field at all, `#[serde]` default-tolerant) is unaffected. **Dropping** the
legacy pair is the breaking step and is deliberately *not* taken here; it
needs its own ADR and a `format_version` gate.

### D3 — delete `indexbot announce`, keep its core

The `announce` subcommand, `cli/announce.py`, its wiring entry and its docs
are removed. `core/regenerate.py`, `core/observe.py` and
`core/verify_claims.py` **stay** — `reconcile`, `validate` and `seed-import`
share them, and they are the read/verify half, not the publish half.

The publish lane is `ocx package announce`. indexbot owns the index side:
validate, classify, gate, label, reconcile, render, generate CI.

## Consequences

**Blast radius, measured rather than assumed — two repos, not four:**

| Repo | Change |
|---|---|
| `ocx-sh/indexbot` | D1–D3 |
| `ocx-sh/index` | ~1.8k package roots rewritten; `schema/root.schema.json`; `.github/maintainers.yml` |
| `ocx-sh/ocx` | **none.** `IndexRoot` (`crates/ocx_lib/src/oci/index/wire.rs`) has no `owners` field, no `deny_unknown_fields`, and `index_root_tolerates_unknown_fields_for_fleet_forward_compat` locks that tolerance. `ocx package announce` passes `owners` through as `serde_json::Value` |
| `ocx-sh/ocx-mirror` | **none.** Preserves `owners` as a human-governed field (`index_write.rs`), never parses its shape |
| `@ocx-sh/catalog` | `Owner` type declaration only — the view-model omits `owners` entirely |

Both `ocx` and `ocx-mirror` carry wrong-shaped `owners` fixtures (`["alice"]`,
`["ocx-sh"]`) that pass only because nothing parses the shape. Each gets a
one-line comment saying so, so the next reader does not mistake the fixture
for the contract.

**Costs accepted:**

- Every root's bytes change. The index repo's `scripts/golden-baseline.sh`
  proves the *rendered* dist is byte-identical across the migration, which is
  the property that matters — the roots are supposed to change.
- Roots are ~2 fields per owner larger. With one or two owners per root this
  is noise against the `tags` map.
- Removing a subcommand is breaking: 0.5.0, not a patch.
