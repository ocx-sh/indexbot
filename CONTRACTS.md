# `indexbot` Module Contracts (Phase 2 Build Wave)

This is the interface spec 16 parallel Sonnet builders implement against. It is
binding: implement these exact signatures. If a contract below is wrong,
underspecified, or blocks you, say so in your work package's `open_questions` —
never silently deviate. Frozen types (`ports.py`, `model.py`) already exist in
`src/indexbot/`; read them first, they are the ground truth for anything this
document summarizes rather than quotes verbatim.

Design authority, in order of precedence for anything this document doesn't
settle: `adr_locked_observation_index_format.md` (wire format, "ADR-1"),
`adr_namespace_policy.md` ("ADR-2"), `adr_index_bot_and_workflow_security.md`
("ADR-4"), `adr_catalog_docs_colocation.md` ("ADR-3"), `plan_index_v1.md`.

## 0. How "pure `core/`" actually works

`core/` modules import nothing from `adapters/` or `httpx` — but several take
a `RegistryPort`/`GitHubPort`/`FilePort`/`ClockPort` argument directly (e.g.
`core/observe.py`'s `registry: RegistryPort` parameter). This is not a
contradiction: **"pure" here means deterministic given its explicit inputs,
including injected ports** — a unit test passes a `tests/fakes/` fake and gets
a 100%-deterministic result; production wiring (`cli/main.py`'s eventual DI,
WP2-M) passes the real `adapters/*` implementation. "No I/O" means no direct
`httpx`/filesystem/`time.time()` call inside the module's own body — every
such effect is reached exclusively through an injected port. This is the same
pattern the existing scaffold already uses for `ClockPort`/`FixedClock`.

## 1. Canonical JSON & digest computation (binding for every module below)

Any function that computes a content digest (`ObservationObject` digests,
`desc.digest` comparisons, CAS filenames) uses this encoding, no exceptions —
two independently-implemented modules computing the same logical content must
produce byte-identical output or dedup (ADR-1 D4) silently breaks:

```python
json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
```
UTF-8 encoded, then `hashlib.sha256(...).hexdigest()`, written as
`f"sha256:{hex}"`. `ObservationObject.platforms` is sorted by
`(platform.architecture, platform.os, platform.os_version or "", platform.variant or "", platform.os_features, platform.features)`
(`core/validate_entry.py`'s `platform_sort_key` — the one shared key every
sorter imports, never a second copy; two platforms differing only in
`os_features`/`features`, e.g. the dual-libc `libc.glibc` vs `libc.musl`
case, must not tie under a shorter key or Python's stable sort would leak
registry manifest-list order into the digest). `os_features`/`features`
are compared as tuples directly, never `",".join(...)`-ed into a string
first — joining is not collision-free (`("a,b",)` and `("a", "b")` both
join to `"a,b"`), which would silently reproduce the same registry-order
dependence this key exists to eliminate
before serialization — registry-returned manifest-list ordering must never
affect the digest. The JSON *shape* serialized is the wire shape from
`schema/observation-object.schema.json` (`os.version`/`os.features` as
literal dotted keys, not a nested `os` object) — `core/validate_entry.py`
owns the `PackageRoot`/`ObservationObject` <-> `dict` codec (§5.6) that every
other module reuses; do not hand-roll a second encoder.

## 2. Test conventions

- **Golden fixtures** (`core/render.py`, WP2-F): `tests/golden/render/<case>/`,
  one directory per named scenario from ADR-4 BD-3's list (`normal`,
  `orphan_pruned`, `yanked_excluded`, `shared_digest_dedup`, `no_desc`,
  `png_only_logo`, `nested_namespace`). Each case directory holds
  `input/` (the `SourcePackage` fixtures, as plain Python data built in the
  test file — no need to serialize/deserialize JSON just to build a fixture)
  and `expected/dist/` (the exact files `build_render_plan`'s returned tuple
  should produce, compared byte-for-byte). Keep fixtures as Python
  literals in the test module unless a case is large enough that a checked-in
  file genuinely reads better — most of these don't need one.
- **respx** (`adapters/ghcr.py`, `adapters/github_api.py`, WP2-C/WP2-D): one
  `respx.mock` route per distinct response class per method
  (200/404/401-then-retry/429-with-Retry-After/5xx-exhausted/malformed-JSON).
  Assert on the *port-level* return/exception, not on respx call internals,
  so the test survives an adapter refactor.
- **hypothesis** (`core/validate_entry.py`'s `parse_package_id`, re-homed
  from the deleted `core/validate_payload.py`): `from_regex(PACKAGE_ID_RE,
  fullmatch=True)` for the acceptance property; a second strategy seeded with
  `..`, absolute paths (`/etc/passwd`), and shell/format-string injection
  tokens for the rejection property; a wall-clock-bounded test (`pytest-timeout`
  is not a declared dependency — use `time.monotonic()` before/after a
  worst-case-length adversarial input and assert the elapsed time is under a
  fixed small bound, e.g. 50ms) proving the length cap makes regex work
  non-catastrophic even for a crafted 140-char input.
- **Idempotency** (`core/regenerate.py`/`core/diff.py`, WP2-B): "run twice,
  second diff empty" — call `regenerate` then `diff` twice in a row with the
  same fake `Observation` set and assert the second `diff` call returns
  `None`, and that no `TagEntry.observed` timestamp changed between the two
  `regenerate` outputs.
- Everywhere: DAMP, self-contained per test — no shared fixture module beyond
  `tests/fakes/`.

## 3. `model.py` / `ports.py` — already implemented, summary only

New since scaffold: `OwnershipProbeResult`, `CommitStatusState`,
`PullRequestInfo` (model.py); `RegistryPort.get_blob`/`probe_ownership`,
`GitHubPort.get_ref_sha`/`commit_files`/`get_pull_request_info`/
`set_commit_status`, `FilePort.read_bytes`/`write_bytes`/`list_files`
(ports.py). Read the docstrings in those files — they are the exception
contract (which raises `KeyError` vs `TransientError` vs `ValidationError`)
and are not repeated here.

Further additions, fork-PR announce revamp (2026-07-18): `PullRequestInfo`
gains `author_login: str = ""`/`author_id: int = 0` (defaulted so every
pre-existing `classify_pr`-only construction site stays unchanged — only
`cli/governance_check.py`'s G-19 gate needs both set for real).
`GitHubPort.open_or_update_pull_request` gains an optional
`head_owner: str | None = None` keyword (cross-repo/fork-PR head,
`f"{head_owner}:{branch}"`, vs. the same-repo plain `branch`).
`GitHubPort` gains `request_reviewers(pr_number, logins)`,
`create_comment(pr_number, body, *, marker)` (idempotent via a hidden HTML
marker), and `create_or_update_issue(*, title, body, labels=None)` (promoted
from an adapter-only capability — see §13 item 4). All three implemented in
`adapters/github_api.py::GitHubApi` and `tests/fakes/__init__.py::FakeGitHub`.

Types referenced below that are **not** in `model.py` (cross-`core/`-module
data, deliberately kept out of the port-boundary file — see `ports.py`'s
module docstring) must be defined by the owning module as a
`@dataclass(frozen=True, slots=True)` exactly as shaped here.

## 4. `core/validate_payload.py` — **removed** (fork-PR announce revamp, 2026-07-18)

This module and its dedicated test file no longer exist. `PACKAGE_ID_MAX_LENGTH`,
`_NAMESPACE_MAX_LENGTH`, `_PACKAGE_MAX_LENGTH`, `_NAMESPACE_SHAPE`,
`_PACKAGE_SHAPE`, `PACKAGE_ID_RE`, and `parse_package_id` re-homed verbatim
(zero behavior change) into `core/validate_entry.py` — see §5, which is now
the single home for both the two-segment package-id grammar and the
N-segment OCI repository grammar (BD-4's two-regex rule still holds: the two
constants stay structurally distinct, only their *file* is shared now). The
original rationale for a standalone module — `parse_package_id` was reached
via `cli/_common.py`'s `read_validated_env`, the `repository_dispatch`
`PACKAGE_ID` env-var-indirection reader (ADR-4 BD-4) — no longer applies:
`cli/announce.py`'s doorbell pipeline (env var, `--validate-only`) retired
entirely in the revamp (owner-confirmed decision set 2026-07-18,
"Fork-PR announce": publishers open PRs from forks under their own GitHub
identity, no index-side credentials, no doorbell). `read_validated_env`
itself is deleted from `cli/_common.py` along with its tests — every
remaining caller of `parse_package_id` (`cli/validate.py`,
`cli/reconcile.py`, `cli/seed_import.py`) now imports it from
`core/validate_entry.py` directly.

## 5. `core/validate_entry.py` (WP2-E)

```python
_COMPONENT = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
OCI_REPOSITORY_RE: Final[re.Pattern[str]] = re.compile(rf"^{_COMPONENT}(?:/{_COMPONENT})*$")

REPOSITORY_HOST_ALLOWLIST: Final[frozenset[str]] = frozenset({"ghcr.io"})  # extend only via reviewed PR
```

`OCI_REPOSITORY_RE` is a **structurally distinct constant** from
`PACKAGE_ID_RE` — never share a compiled pattern or a "guess which shape"
helper between the two (ADR-4 BD-4, the regclient/regsync failure mode).

Functions (each raises `ValidationError` on failure, never returns a bool):

- `check_name_matches_path(package_id: PackageId, root: PackageRoot) -> None`
  — G-02: `root.name == f"ocx.sh/{package_id.namespace}/{package_id.package}"`.
- `check_superseded_by(root: PackageRoot) -> None` — no-op when
  `root.superseded_by is None`; otherwise the value must shape-validate as a
  `<namespace>/<package>` id (reusing this module's own `parse_package_id` —
  never a second hand-rolled regex) and must not name `root` itself.
  Deliberately does **not** check `root.status` coupling (a package can name
  a successor while still `active`) nor whether the named successor exists
  or is reserved (a dangling/not-yet-claimed successor is allowed, like
  `deprecated_message`'s free-text pointer).
- `check_repository_allowlisted(repository: str) -> None` — G-03. Parses the
  `oci://<host>/<path>` URI (stdlib `urllib.parse`, no regex needed for the
  scheme/host split) and checks `host in REPOSITORY_HOST_ALLOWLIST`. **Must
  run before any `RegistryPort` call** — SSRF ordering, BD-1.
- `check_repository_shape(repository: str) -> None` — validates the
  `<path>` portion of `oci://<host>/<path>` against `OCI_REPOSITORY_RE`
  (N-segment grammar — never `PACKAGE_ID_RE`).
- `parse_digest(raw: str) -> str` — `re.fullmatch(r"sha256:[a-f0-9]{64}", raw)`
  or `ValidationError`. Every digest-shaped string anywhere in the bot
  (`TagEntry.content`, `PlatformEntry.digest`, `Desc.digest`/`.readme`/
  `.logo`) is validated through this one function before it is ever used to
  build a filesystem path — digest-hex `fullmatch` before path join, no
  exceptions.
- `check_digest_self_consistent(digest: str, object_bytes: bytes) -> None`
  (fork-PR announce revamp, 2026-07-18) — the general form: recomputes sha256
  of `object_bytes` (§1's canonical form — the object was already serialized
  canonically when written, so this is a byte-equality check, not a
  re-serialization) and compares to `digest`; mismatch is `AnomalyError`
  (this is CAS integrity, not a routine validation failure — the file's name
  lies about its own content). Any claimed digest string works here, not
  only a `TagEntry`'s — `cli/validate.py`'s blanket per-file CAS scan and
  `core/verify_claims.py`'s desc-blob hash check both need this, closing the
  byte-exact-discipline gap where only tag digests were ever verified.
- `check_content_digest_self_consistent(tag: TagEntry, object_bytes: bytes) -> None`
  — thin `TagEntry`-shaped wrapper over `check_digest_self_consistent(tag.content,
  object_bytes)`, kept for its pre-existing callers/tests.
- `check_no_dangling_references(root: PackageRoot, cas_digests: frozenset[str]) -> None`
  — every `TagEntry.content` and `Desc.readme`/`Desc.logo` (when `desc` is
  not `None`) must appear in `cas_digests` (the set of digests actually
  present under this package's `o/sha256/` tree, as enumerated by the caller
  via `FilePort.list_files`). Raises `AnomalyError` per missing reference —
  a root pointing at a CAS object that doesn't exist is corruption, not a
  routine PR mistake.
- `parse_package_root(raw: bytes) -> PackageRoot` /
  `serialize_package_root(root: PackageRoot) -> bytes` — the `dict`<->
  dataclass codec every other module reuses (§1). `serialize_package_root`
  produces the exact bytes committed to `p/<ns>/<pkg>.json` — pretty-printed
  (`json.dumps(..., indent=2, sort_keys=False)` preserving the field order
  `model.PackageRoot` declares them in, matching `schema/root.schema.json`'s
  `required` order) plus a trailing newline, **not** the canonical
  minified form from §1 (§1's canonical form is only for content-addressed
  CAS objects, which must dedup; the human-diffable root is optimized for PR
  review, not digest stability — the root's own bytes are never digested,
  only referenced by `TagEntry.content`, which points at an
  `ObservationObject`, not at the root itself). `upstream: None` -> the
  `"upstream"` key is **omitted** from the dict entirely (schema forbids
  `null` there, ADR-2 ND-9); `superseded_by: None` -> the `"superseded_by"`
  key is likewise **omitted** entirely (same omit-when-absent contract);
  `desc: None` -> `"desc": null` is written (schema requires the key, allows
  `null`, ADR-1 D6). `parse_package_root`
  raises `ValidationError` on any structurally malformed input (missing
  required key, wrong JSON type) — it does not re-validate shape-schema
  concerns already covered by `check-jsonschema` (regex patterns, enum
  membership); it only needs to not crash on well-formed-but-unexpected
  JSON and to fail loudly (never partially construct a `PackageRoot`) on
  malformed JSON.
- `parse_observation_object(raw: bytes) -> ObservationObject` /
  `serialize_observation_object(obj: ObservationObject) -> bytes` — same
  codec relationship, but `serialize_observation_object` **is** §1's
  canonical minified form (this is the CAS-digested one).

`registry_checks` (network — G-15, digest-scope):

- `check_digest_in_scope(repository: str, digest: str, registry: RegistryPort) -> None`
  — `registry.get_manifest(repository, digest)`; a `KeyError` (404) means the
  claimed content digest does not actually exist on the physical repo ->
  re-raise as `ValidationError` (a claim about registry content that isn't
  true is a validation failure, not an anomaly — nothing was ever
  legitimately observed to mutate).
- `check_ownership(repository: str, expected_name: str, registry: RegistryPort) -> OwnershipProbeResult`
  — thin pass-through to `registry.probe_ownership`. The caller (`cli/validate.py`)
  decides disposition: `"mismatch"` -> `ValidationError` (block); `"unconfirmed"`
  -> **do not raise** — return the result so the caller can attach a WARN
  annotation to the PR (`GitHubPort.add_labels` with something like
  `ownership-unconfirmed`, or a PR comment — `cli/validate.py`'s call,
  WP2-H..L). Never silently treat `"unconfirmed"` as `"confirmed"`.

## 6. `core/version_order.py` (WP2-F)

Ported from `ocx/scripts/catalog-generate.py`'s `find_latest_version` (real
source read for this stage — verified no separate "yank-exclusion" code
exists there; that logic is new, per ADR-1's yank semantics, not a port):

```python
_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:([a-z][a-z0-9.]*)-)?((0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?)?)$"
)

def is_full_release_version(tag: str) -> bool:
    """True iff `tag` is an unprefixed, fully-qualified 3-component version
    (`_VERSION_RE` matches, group(1) [variant prefix] is `None`, AND groups
    4 and 5 [minor, patch] are both not `None`). `latest`, a bare major
    (`3`), a major.minor (`3.28`), and any variant-prefixed tag are all
    `False` — see `core/anomaly.py`'s use of this predicate (§7) for why the
    distinction matters.
    """

def find_latest_version(tags: Mapping[str, TagEntry]) -> str | None:
    """Highest version among tags that are (a) not "latest", (b) unprefixed
    (`m.group(1) is None` — variant tags are skipped, matching the ported
    function's original behavior verbatim), and (c) not yanked
    (`tags[t].yanked is None` — new: ADR-1 yank semantics, a yanked tag must
    never be selected as the displayed/default version). Comparison is by
    the parsed `(major, minor, patch)` int tuple, missing components treated
    as absent (not zero) for tuple comparison purposes, matching the ported
    function's `tuple(int(x) for x in m.group(2).split(".") if x)` behavior
    exactly. Returns `None` if no eligible tag exists.
    """
```

## 7. `core/observe.py` / `core/regenerate.py` / `core/diff.py` / `core/anomaly.py` / `core/desc.py` / `core/backoff.py` (WP2-B)

These six ship together (one work package) but are listed separately since
several are consumed by other WPs built in parallel.

### `core/backoff.py`

```python
@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0

def is_retryable_status(status_code: int) -> bool:
    """True for 429 or any 5xx. False for everything else, including other
    4xx (401/404 are permanent failures for a given request, never retried
    by this policy — 401 gets one token-refresh-and-retry inside
    `adapters/ghcr.py`, which is a different mechanism, not backoff)."""

def delay_seconds(
    attempt: int, policy: BackoffPolicy, *, jitter: float, retry_after: float | None = None
) -> float:
    """`attempt` is 1-indexed. If `retry_after` is given and positive, it
    wins outright (the server said exactly how long to wait — G-10).
    Otherwise: `min(policy.max_delay_seconds, policy.base_delay_seconds * 2 ** (attempt - 1)) * (0.5 + jitter)`,
    `jitter` in `[0, 1)` supplied by the caller (`adapters/ghcr.py` passes
    `random.random()`; tests pass a fixed float) — keeps this function
    itself deterministic and trivially 100%-coverable without mocking
    `random` or `time`.
    """
```

The retry **loop** (attempt counting, calling `httpx`, sleeping, deciding
when `policy.max_attempts` is exhausted and raising `TransientError`) lives
in `adapters/ghcr.py` — it is imperative-shell code that happens to consult
this pure module's two functions for its decisions. Do not move the loop
into `core/backoff.py`; that would require mocking `time.sleep`/`httpx` to
test it, defeating the whole point of the split.

### `core/observe.py`

```python
@dataclass(frozen=True, slots=True)
class Observation:
    """One tag's freshly observed state. Input to regenerate/anomaly."""
    tag: str
    content_digest: str          # sha256:<hex>, §1 canonical form of `object`
    object: ObservationObject

def observe_one_tag(repository: str, tag: str, registry: RegistryPort) -> Observation | None:
    """One tag's freshly observed state, or `None` if `tag` no longer exists
    on `repository` (a real 404). Same manifest-shape handling as `observe`
    below, extracted (fork-PR announce revamp, 2026-07-18) so a caller that
    already knows which *specific* tags it cares about — `core/verify_claims.py`
    re-deriving one claimed tag, `cli/announce.py` observing only the
    publisher's curated tag set — never has to call `registry.list_tags()`
    first just to reach a single tag's manifest.
    """

def observe(repository: str, registry: RegistryPort) -> tuple[Observation, ...]:
    """One `Observation` per `registry.list_tags(repository)` entry, via
    `observe_one_tag` (zero behavior change from the pre-revamp inline
    version). For each tag, `registry.get_manifest(repository, tag)` returns
    either an OCI image manifest (single platform) or an image index
    (multi-platform) — distinguish by the `mediaType`/`manifests` key per the
    OCI image-spec; a bare manifest becomes a one-entry `platforms[]` (its own
    `platform`/`config.platform` field — image manifests carry platform
    info directly, not per the index shape); an index's `manifests[]`
    entries each become one `PlatformEntry`. `platforms[]` is sorted and
    digested per §1. A tag whose manifest fetch raises `KeyError` (fetched
    but vanished between `list_tags` and `get_manifest` — a real registry
    race) is **skipped**, not fatal — `observe_one_tag` returning `None`. A
    `TransientError` from either call propagates uncaught (the whole
    `observe()` call fails transient, per BD-2 — no partial-tag
    silently-skipped-on-backoff-exhaustion semantics; that's different from
    the vanished-tag case above, which is a real 404, not exhausted
    backoff).
    """
```

### `core/verify_claims.py` (fork-PR announce revamp, 2026-07-18 — new)

```python
FindingKind = Literal[
    "tag-missing-upstream", "digest-mismatch",
    "cas-object-missing", "cas-object-hash-mismatch",
    "desc-blob-missing", "desc-blob-hash-mismatch",
]

@dataclass(frozen=True, slots=True)
class ClaimFinding:
    package_id: PackageId
    kind: FindingKind
    detail: str  # claimed tag name, or "desc.readme"/"desc.logo"

def verify_claims(
    package_id: PackageId,
    root: PackageRoot,
    cas_object_bytes: Mapping[str, bytes],
    registry: RegistryPort,
) -> tuple[ClaimFinding, ...]:
```

Re-derives every *claimed* tag/desc-blob digest in `root` individually from
`registry` truth — **subset semantics**, never a full-set equality against
`registry.list_tags()` (an owner's curated `tags` map may legitimately be a
subset of what the registry carries; that is the entire point of owner
curation — decision-set item 2, "announce is the only add/remove
authority"). Per tag: `observe_one_tag(root.repository, tag, registry)`
returning `None` -> `"tag-missing-upstream"`; a different `content_digest`
-> `"digest-mismatch"`; the claimed digest missing from/not hashing to
`cas_object_bytes` -> `"cas-object-missing"`/`"cas-object-hash-mismatch"`.
`root.desc.readme`/`.logo`, when set, get the identical CAS-hash check
(missing/mismatch -> `"desc-blob-missing"`/`"desc-blob-hash-mismatch"`) —
closing the gap where only tag digests were ever byte-verified (byte-exact
discipline). Pure — returns findings, **never raises**; the caller decides
disposition. Two callers, two dispositions for the same taxonomy:
`cli/validate.py`'s unprivileged PR gate treats any finding as a
`ValidationError` (reject the PR — nothing was ever legitimately observed to
mutate, the claim just isn't true right now); `cli/reconcile.py`'s
verify-only nightly sweep escalates every finding kind to its `AnomalyError`
exit-65 outcome **except** a `"digest-mismatch"` on a floating (non-pinned)
tag and a `"tag-missing-upstream"` on a *yanked* tag (ADR-6 FP-2/FP-3 — yank
is grace, an explicit owner-authorized exemption from the
registry-existence check; everything else vanished-upstream is an anomaly,
not a silent drop) — see §12's `cli/reconcile.py` entry for the full
disposition table: floating-tag drift is expected cascade behavior, and a
still-*pinned*-tag mutation is caught by the reused
`core/anomaly.py::check_tag_mutations` instead, not by `verify_claims`.

### `core/desc.py`

Ground truth for the `__ocx.desc` artifact (read from `ocx/crates/ocx_lib`'s
`oci/client.rs::pull_description` and `oci/annotations.rs` — not guessed):

- Tag name: literal `"__ocx.desc"`.
- Manifest: a single OCI **image manifest** (never an image index) with
  `artifactType == "application/vnd.sh.ocx.description.v1"`.
- `manifest.layers[]`: exactly one layer with `mediaType == "application/markdown"`
  (the readme — **required**, a description manifest with no markdown layer
  is malformed) and at most one layer with `mediaType` `"image/png"` or
  `"image/svg+xml"` (the logo — optional). Layer content is fetched via
  `RegistryPort.get_blob(repository, layer.digest)`.
- `manifest.annotations` (**manifest-level**, not layer-level):
  `org.opencontainers.image.title` (title), `org.opencontainers.image.description`
  (description), `sh.ocx.keywords` (comma-separated string — split on `,`,
  strip whitespace, drop empty segments, matching
  `ocx/scripts/catalog-generate.py`'s `parse_keywords` exactly).
- Readme/logo bytes are copied **verbatim** — no frontmatter re-parsing (that
  machinery, `ocx_lib::package::description::parse_readme`, is publish-side
  only; the index bot only ever fetches).

```python
@dataclass(frozen=True, slots=True)
class DescUpdate:
    """Non-`None` return of `check_desc_change` — what the caller persists."""
    desc: Desc
    readme_bytes: bytes | None
    logo_bytes: bytes | None

def check_desc_change(
    repository: str, current: Desc | None, registry: RegistryPort
) -> DescUpdate | None:
    """Compares `registry.get_desc_tag_digest(repository)` against
    `current.digest` (or `None` if `current is None`). Returns `None`
    (no change — caller keeps `current` verbatim, writes nothing new) if
    they match, including both-absent. Otherwise fetches the `__ocx.desc`
    manifest and its layers per the format above, builds the new `Desc`
    (`digest` = the observed `__ocx.desc` tag digest itself, not a
    recomputed content hash — this is a floating-tag comparison, D6, not a
    CAS digest), and returns a `DescUpdate` whose `readme_bytes`/
    `logo_bytes` the caller writes as this package's new CAS objects at
    `o/sha256/<hex>.<ext>` (`hex` = sha256 of those exact bytes per §1;
    `.md` for the readme, `.svg`/`.png` for the logo per its layer media
    type). `desc.readme`/`desc.logo` in the returned `Desc` are those same
    `sha256:<hex>` digest strings. A missing logo layer -> `logo_bytes = None`,
    `desc.logo = None`. A missing `sh.ocx.keywords` annotation ->
    `desc.keywords = ()`.
    """
```

### `core/regenerate.py`

```python
def regenerate(
    current: PackageRoot, observations: tuple[Observation, ...], desc: Desc | None, clock: ClockPort
) -> PackageRoot:
```

- `current` is **required, never `None`** — a package_id with no committed
  root is a validation error the caller (`cli/announce.py`/`cli/reconcile.py`)
  raises *before* calling `regenerate` (namespace claiming, ADR-2 ND-5, is a
  separate human-PR flow that already commits a root with empty `tags`
  before the first `announce` ever runs — `regenerate` never synthesizes a
  root from scratch).
- Human-governed fields (`name`, `repository`, `owners`, `status`,
  `deprecated_message`, `created`, `upstream`, `superseded_by`) are carried
  over **verbatim** from `current` — never regenerated (G-09).
- `desc`: pass `current.desc` unchanged when `core/desc.py` found no change,
  or the new `Desc` from a non-`None` `DescUpdate.desc` when it did.
  `regenerate` does not call `core/desc.py` itself — the caller composes
  both.
- `tags`: rebuilt entry-by-entry from `observations`. A tag whose
  `content_digest` equals `current.tags[tag].content` keeps that entry's
  `observed` timestamp **unchanged** (no gratuitous timestamp churn on a
  no-op re-observe — this is what makes "run twice, second diff empty"
  hold, §2's required idempotency test). A new or changed-content tag gets
  `observed = clock.now_iso8601()`. A tag present in `current.tags` but
  absent from `observations` (removed upstream) is **dropped**.
- `yanked`: an existing `TagEntry.yanked` marker survives untouched
  (human-governed, G-05) even if that tag's content also changed this run.

  **Open question** (neither ADR states this explicitly): does a
  re-published digest under a yanked tag name clear the yank? This
  contract's default is **no** — preserve `yanked` regardless of content
  change. Confirm with the owner before Phase 3.
- A tag vanishing from the registry entirely (present in `current`, absent
  from `observations`) is **not itself an anomaly** — `core/anomaly.py`
  only checks digest *mutation* on a still-present pinned tag, not
  disappearance. **Open question**: is silent tag disappearance actually
  fine, or should reconcile flag it too? Not decided by either ADR; flagged
  here rather than silently assumed safe.

### `core/diff.py`

```python
@dataclass(frozen=True, slots=True)
class Patch:
    package_id: PackageId
    root: PackageRoot                                       # target — write verbatim (validate_entry.serialize_package_root)
    new_objects: tuple[tuple[str, ObservationObject], ...]   # (digest, object) pairs not already reachable from `current`
    summary: str                                             # one-line PR-body fragment, e.g. "+3.29.0, ~latest -> sha256:bbbb"

def diff(current: PackageRoot, target: PackageRoot) -> Patch | None:
    """`None` iff `current == target` structurally (dataclass equality —
    both are frozen, so this is a plain `==`) — BD-2's `ExitCode.OK` no-op
    case. Otherwise a `Patch`. `new_objects` is target's tags whose content
    digest does not appear anywhere in `current.tags` — already-existing
    objects (shared digest / cascade aliasing, ADR-1 D3) are excluded so
    `cli/announce.py` never re-writes a CAS object that's already committed.
    """

ChangeClass = Literal["new-package", "refresh", "human-review-required"]

def classify_change(before: PackageRoot | None, after: PackageRoot) -> ChangeClass:
    """`cli/classify_pr.py`'s core. `before` is the base-ref root, `None` if
    the PR added a brand-new `p/<ns>/<pkg>.json` (the path did not exist at
    the base ref — G-04). `before is None` -> always `"new-package"`.

    Otherwise the machine lane is narrow by design (fork-PR announce revamp,
    2026-07-18): **any field outside `tags`/`desc` changing is
    `"human-review-required"`** — every governance field
    `product-context.md` lists is human-authored, never auto-mergeable.
    Concretely: `repository`, `owners`, `status`, `deprecated_message`,
    `created`, `upstream`, or `superseded_by` differing -> `"human-review-required"`,
    OR any tag present in both `before.tags` and `after.tags` has a
    different `yanked` value (G-05's expanded key set, ADR-4 disposition
    table) — else `"refresh"`. `name` is not checked here (pinned by
    `check_name_matches_path` instead — a structural invariant, not a
    governance-vs-machine distinction); `desc` is not checked here either
    (bot-derived from the registry's `__ocx.desc` tag, `core/desc.py` — not
    human-authored, stays in the machine lane alongside `tags`).
    """
```

### `core/anomaly.py`

```python
@dataclass(frozen=True, slots=True)
class AnomalyFinding:
    package_id: PackageId
    tag: str
    committed_content: str
    fresh_content: str

def check_tag_mutations(
    package_id: PackageId, committed: PackageRoot, fresh: tuple[Observation, ...]
) -> tuple[AnomalyFinding, ...]:
    """Empty tuple = clean. For every tag present in both `committed.tags`
    and `fresh` that `core/version_order.is_full_release_version` classifies
    `True` (pinned — an exact, unprefixed `X.Y.Z`), a different content
    digest between `committed` and `fresh` is one `AnomalyFinding`. Tags
    classified `False` (`latest`, partial versions, variant-prefixed) are
    floating and are never flagged regardless of digest change — that is
    the expected cascade-push behavior (ADR-1 D2/D3).

    **Open question, flagged loudly rather than silently resolved**: neither
    ADR-1 nor ADR-4 states the pinned-vs-floating predicate explicitly.
    ADR-4's G-13 disposition table delegates it to ADR-1, but ADR-1 D2/D3
    only describes the cascade *convention*, not which tag *shapes* are
    anomaly-checked. This contract's default (exact `X.Y.Z` only is pinned)
    is the Contracts stage's best-effort reading, not a confirmed decision —
    a wrong default here either misses real tamper (too permissive) or
    fires false-positive anomalies on legitimate cascade pushes (too
    strict). Confirm with the owner before Phase 3's E2E gate (ADR-4's own
    Validation checklist already has an open item for exactly this).
    """
```

Returning findings (not raising) lets `cli/reconcile.py` implement the
plan's "partial-success semantics" (clean-subset PR + one anomaly issue
listing every finding + exit 65) — `check_tag_mutations` itself never
raises `AnomalyError`; the CLI layer maps a non-empty result to that outcome.

## 8. `core/render.py` (WP2-F, reshaped by `plan_site_redesign` WP-bot)

`core/catalog_md.py` — this section's original wrapper-page-Markdown
module — is **deleted**. `plan_site_redesign` retired bot-generated
per-package wrapper pages in favor of dynamic routes
(`site/src/[ns]/[pkg].paths.ts`, globbing the committed `p/*/*.json` tree
directly at VitePress build time — see `adr_catalog_docs_colocation.md`
Amendment A1 and `site/README.md`). `core/render.py` now emits exactly one
output tree; `cas_relpath` (the CAS path-building helper the deleted module
also used) relocated to `core/validate_entry.py`, alongside
`cli/reconcile.py`'s existing import of it.

```python
@dataclass(frozen=True, slots=True)
class SourcePackage:
    """One package's fully-loaded source-tree state — cli/render.py's input
    unit, assembled via FilePort reads (list_files over `p/`, read_text per
    root, read_bytes per CAS object)."""
    package_id: PackageId
    root: PackageRoot          # parsed — drives the reachability walk
    root_raw: bytes            # exact p/<ns>/<pkg>.json source bytes — copied verbatim into dist, never re-serialized
    content_by_digest: dict[str, bytes]  # digest -> raw CAS bytes, this package's CAS only (key/extension bookkeeping is WP2-F's internal choice — see note below)

@dataclass(frozen=True, slots=True)
class FileWrite:
    path: str            # relative to the dist output root (`--out`)
    content: str | bytes

def build_render_plan(packages: Sequence[SourcePackage], *, format_version: int = 1) -> tuple[FileWrite, ...]:
```

Pure (§0). No `RenderPlan` wrapper dataclass any more — `build_render_plan`
returns the flat dist-tree file list directly, since there is only ever one
output tree (`--out`, applied by `cli/render.py` after `site:build`'s
VitePress build completes — see `taskfile.yml`'s `render:build` task and its
`emptyOutDir` footgun comment).

Reachability walk per package: only `tags[*].content` digests of *live*
(non-yanked) tags, and, transitively, `desc.readme`/`desc.logo` digests, are
copied — CAS objects orphaned by a repointed or yanked tag are pruning
candidates (ADR-1 D8, **deployment artifact only**, never source-tree git
history). A yanked tag's content is pruned **only if unreachable from every
other live tag** — yanking does not itself force pruning while another tag
still shares the digest (emergent aliasing, ADR-1 D3, applies to
reachability too).

Returned file list:
- `config.json`: `{"format_version": format_version}`, exactly (D7 —
  nothing else, ever).
- One `p/<namespace>/<package>.json` per package: `content = source.root_raw`
  verbatim (never re-serialize through the dataclass — see §5's rationale).
- Every reachable `p/<namespace>/<package>/o/sha256/<hex>.<ext>` — copied
  verbatim from `content_by_digest`.
- `c/index.json`: `{"format_version": format_version, "packages": {"<ns>/<pkg>": "sha256:<hex>", ...}}`
  — one entry per package in `ordered`, keyed on the bare `<namespace>/<package>`
  id (not the `ocx.sh/`-prefixed `name`). The digest is `sha256` of
  `source.root_raw`'s **exact committed bytes** — explicitly **not** a
  re-serialization through §1's canonical form (root bytes are never digested
  for wire-contract purposes elsewhere either, same rationale as §5's
  `serialize_package_root`).
- `data/catalog/catalog.json` — the catalog-grid view-model, frozen shape
  (`plan_site_redesign`), referencing logo/readme blobs by their CAS URL
  rather than duplicating blob bytes into `/data/catalog` (ADR-3's explicit
  divergence from `ocx-sh/ocx`'s website). **Not** wire contract (ADR-1 D2,
  ADR-3):
  ```jsonc
  {
    "generated": "…Z" | null,   // lexicographic max over every tag's observed
                                 // + yanked.at value across every package;
                                 // null if no package has ever carried a tag.
                                 // No datetime import — a wall-clock value
                                 // would break `render --check` idempotency.
    "packages": [{              // sorted by package_id
      "namespace": "kitware", "package": "cmake", "name": "ocx.sh/kitware/cmake",
      "status": "active" | "deprecated" | "yanked",
      "deprecatedMessage": string | null,
      "supersededBy": string | null,
      "title": string, "description": string, "keywords": string[],
      "latestVersion": string | null,
      "tagCount": number,        // non-yanked tag count
      "platforms": string[],     // "<os>/<architecture>" union across every
                                  // non-yanked tag's observation object,
                                  // deduped + sorted
      "logoUrl": string | null, "readmeUrl": string | null   // pre-resolved CAS paths
    }]
  }
  ```

Note on `content_by_digest` keying: a CAS digest alone does not carry its
file extension (`.json` vs `.md` vs `.svg`/`.png`) — the extension is only
known from the filename `cli/render.py` discovers via `FilePort.list_files`.
Key the map however is convenient (e.g. `"sha256:<hex>.<ext>"`, or a
`(digest, ext)` tuple) as long as the reachability walk itself keys purely
on the bare `sha256:<hex>` digest strings stored in `TagEntry.content` /
`Desc.readme` / `Desc.logo` — that part is frozen, the key encoding is not.

## 9. `adapters/ghcr.py` (WP2-C)

Implements `RegistryPort`. Bearer-token dance: GHCR anonymous pull tokens
via `https://ghcr.io/token?...` — fetch once per repository, cache for the
adapter instance's lifetime, refresh once (not counted against
`BackoffPolicy.max_attempts`) on a single 401, fail with `TransientError` on
a second consecutive 401 for the same request (a persistent auth failure is
not a backoff-retryable condition, but is also not a `ValidationError` — the
adapter couldn't complete the read, full stop).

Manifest/blob fetch retry loop (the imperative-shell half of §7's
`core/backoff.py` split): on each `httpx` call, if the response status
satisfies `backoff.is_retryable_status`, sleep
`backoff.delay_seconds(attempt, policy, jitter=random.random(), retry_after=parsed_retry_after_header)`
(via `time.sleep`) and retry, up to `policy.max_attempts`; on exhaustion
raise `TransientError`. A malformed-JSON body on an otherwise-200 response
is **not** retryable — raise a plain `ValueError`-derived parse error
(propagates as an unhandled bug per `cli/main.py`'s contract, since a 200
with unparseable JSON from GHCR is not a condition the bot has a defined
recovery for).

`list_tags`: paginate GHCR's `tags/list?n=&last=` — bounded pagination (a
hard cap, e.g. 10,000 pages, converted to `TransientError` if ever hit,
rather than an unbounded loop).

## 10. `adapters/github_api.py` (WP2-D)

Implements `GitHubPort`. REST for contents/refs/PRs/labels/commit-status,
GraphQL only for `enablePullRequestAutoMerge` (the one mutation with no REST
equivalent). `commit_files` uses the Git Data API (create tree from
`base_sha`'s tree + `files`, create commit, update ref with
`force=False` — GitHub itself then supplies the "ref moved" 422/409 that
this adapter converts to `TransientError`, matching `ports.py`'s documented
contract). `open_or_update_pull_request` is idempotent per branch — GitHub's
"list PRs for this branch" REST call first, create only if none exists,
otherwise return the existing number unchanged (never edits title/body on
the update path unless they actually differ, to avoid a no-op PR-edit event
storm).

## 11. `adapters/local_files.py` / `adapters/system_clock.py` (WP2-G)

`local_files.py`: every method resolves `path` against a fixed root
(constructor argument, e.g. the repo checkout root) via `Path(root, path).resolve()`
and raises `ValidationError` if the resolved path is not `.is_relative_to(root)`
(catches both `..`-traversal and absolute-path attempts in one check, per
ports.py's documented contract) **before** touching the filesystem.
`list_files(prefix)` uses `Path.rglob("*")` filtered to files, returned as
`/`-joined POSIX-style relative strings (not OS-native `os.sep`) so output is
stable across platforms and matches `InMemoryFiles`' fake behavior exactly.

`system_clock.py`: `datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")` — one
line, no configuration surface.

## 12. `cli/*.py` (WP2-H..M)

Each subcommand module exposes one function matching `cli/main.py`'s
existing `_DISPATCH` shape: `def run(args: argparse.Namespace) -> ExitCode`.
Registration (`_DISPATCH["announce"] = announce.run`, plus the matching
`subparsers.add_parser(...)` args) is WP2-M's production-wiring job, done
last, once every subcommand module exists — **do not** edit `cli/main.py`'s
`_build_parser`/`_DISPATCH` from an individual WP2-H..L work package; land
your module's `run` function and its own tests, leave wiring to WP2-M.

- **`cli/announce.py`** (fully repurposed, fork-PR announce revamp,
  2026-07-18 — no longer a `repository_dispatch` doorbell target): a
  publisher reference tool. `--package` (required) + `--tags`/`--tags-file`
  (mutually exclusive, one required) + `--out`/`--fork` (mutually exclusive,
  one required) + `--index-repo` (default `ocx-sh/index`) +
  `--yank`/`--unyank`/`--yank-reason`. Pipeline: resolve the curated tag set
  -> read the current root from the index repo at `main`
  (`GitHubPort.get_file_contents`, via a keyword-only `index_github` port —
  unauthenticated is fine for `--out`; missing root -> `ValidationError`,
  "unclaimed namespace — new packages go through the human lane") ->
  `check_repository_allowlisted` (SSRF ordering) -> `observe_one_tag` once
  per curated tag (a tag that does not resolve -> hard `ValidationError`,
  never silently dropped — a publisher typo) -> `desc.check_desc_change` ->
  `regenerate` (owner curation: the curated observed set *is* the new `tags`
  map — `core/regenerate.py`'s existing "observations are the universe,
  absent means removed" semantics already gives exactly this add/remove
  authority, no core change needed) -> `--yank`/`--unyank` marker toggles ->
  build root + CAS bytes -> `--out`: write via `FilePort` under the wire
  paths; `--fork`: `commit_files` against a *second*, keyword-only
  `fork_github` port scoped to `--fork`, then `open_or_update_pull_request`
  on `index_github` with the new `head_owner` parameter (§3) set to the
  fork's owner. No index-side credential is ever read by this module itself
  — `cli/_wiring.py` decides token presence per port. Server-side privileged
  verification (G-19 ownership, claim re-derivation) happens in CI, never
  here — see `cli/governance_check.py` and `cli/validate.py` below.
- **`cli/reconcile.py`** (rewritten verify-only, fork-PR announce revamp,
  2026-07-18 — owner-confirmed decision set "Verify-only reconcile"):
  `FilePort.list_files("p/")` to enumerate every `*.json` root (excluding
  CAS subtrees, unchanged glob rule) -> per package,
  `core/verify_claims.py::verify_claims` re-derives every claimed tag/desc
  blob from registry truth, plus `core/anomaly.py::check_tag_mutations`
  (reused verbatim) for pinned-tag mutation detection. **Never writes to
  `p/` at all** — no regenerate, no diff, no commit, no PR; the `--dry-run`
  flag this module used to carry is gone entirely (verify-only is always
  "dry"). Escalation (which findings raise `AnomalyError`, exit 65):
  `check_tag_mutations`'s pinned-tag mutations always escalate;
  `verify_claims`'s `"cas-object-*"`/`"desc-blob-*"` findings always escalate
  (structural CAS integrity, independent of tag semantics — the same
  unconditional treatment `check_no_dangling_references`/
  `check_digest_self_consistent` already give); `"digest-mismatch"` does
  **not** escalate on its own (floating-tag drift is expected cascade
  behavior, ADR-1 D2/D3, and that same digest mismatch on a *pinned* tag is
  already caught by the reused `check_tag_mutations` — avoids
  double-flagging one phenomenon two ways). `"tag-missing-upstream"` **does**
  escalate (ADR-6 FP-2/FP-3 — a decided rule, no longer an open question)
  *unless* the committed `TagEntry.yanked is not None` for that tag: yank is
  grace, an explicit owner-authorized exemption from the registry-existence
  check — `_PackageReport` carries the committed root's yanked-tag names
  precisely so `_escalating_findings` can tell a yanked-and-vanished tag
  apart from a plain silent drop. A non-empty escalating-finding set
  opens/updates one anomaly issue via `GitHubPort.create_or_update_issue`
  (promoted onto the port this stage, see §3/§10) before raising.
- **`cli/validate.py`** (extended, fork-PR announce revamp — byte-exact
  discipline): takes changed-file paths as CLI positional args (unchanged).
  New, before the existing structural gauntlet: parse the committed root
  bytes, re-serialize via `validate_entry.serialize_package_root`, and
  byte-compare against the committed bytes — a mismatch is
  `ValidationError`, "committed bytes are not the canonical root
  serialization" (CI re-derives the PR's own claimed canonical form; a
  publisher's tooling is expected to already emit exactly this form).
  Also new: every committed CAS file under the package's `o/sha256/` tree
  (tag objects **and** desc readme/logo blobs alike, not only the ones a
  tag/desc field references) is hash-checked against its own filename-declared
  digest via `validate_entry.check_digest_self_consistent` — closes the gap
  where only referenced tag objects were ever byte-verified. Unless
  `--offline`: in addition to the existing `check_digest_in_scope`/
  `check_ownership` G-15 checks, wires `core/verify_claims.py::verify_claims`
  — any finding -> `ValidationError` (a claim about registry content that
  isn't true right now is this PR's problem, not an anomaly against
  committed history; contrast `cli/reconcile.py`'s disposition for the
  identical finding taxonomy). `"mismatch"` or any `ValidationError` -> exit
  1. `"unconfirmed"` -> print a WARN to stderr, exit 0 (ADR-4 Risk 2,
  unchanged).
- **`cli/render.py`** (reshaped by `plan_site_redesign` WP-bot): `FilePort.list_files("p/")`
  under `--index-dir` -> parse every root + every CAS object into
  `SourcePackage` -> `render.build_render_plan` -> write the returned file
  tuple under `--out`. `--out` is `required=True` at the argparse layer
  (`cli/main.py`'s `_add_render_arguments` owns the missing-flag usage
  error, so this module never raises its own `ValidationError` for it);
  `--site-dist` (the old wrapper-pages target) is gone — this CLI
  subcommand emits exactly one output tree now, so there is no second
  invocation or `--phase` split. **This subcommand does not itself invoke
  the VitePress build** — that's `render-deploy.yml`'s job
  (`task render:build`), which runs `site:build` first (dynamic routes glob
  `p/*/*.json` directly, no wrapper-page pre-emission needed) and
  `indexbot render --out` second, into the same `emptyOutDir`-wiped tree.
  `--check` computes the plan and reports drift against `--out` without
  writing, `ExitCode.VALIDATION_FAILURE` on drift.
- **`cli/seed_import.py`**: reads local `CATALOG.md` (title/description/
  keywords — frontmatter shape TBD by whoever writes this WP; note the
  precedent in `ocx_lib::package::description::Frontmatter`, §7's desc.py
  section, if useful) + `logo.svg`/`logo.png` + `mirror.yml` via `FilePort`,
  then `observe` against the live registry to build the initial `tags` map.
  **Open question / dependency gap**: `mirror.yml` implies YAML parsing;
  `bot/pyproject.toml` has no YAML dependency (`httpx` is the only runtime
  dep, per BD-1's minimal-footprint driver) and this stage may not edit
  `pyproject.toml`. Flag the missing `pyyaml`/`ruamel.yaml` dev-or-runtime
  dependency in this WP's `open_questions` rather than adding it unilaterally
  — or parse `mirror.yml` with a deliberately tiny hand-rolled `key: value`
  reader if its real shape turns out to be that simple (confirm shape
  against actual seed data before choosing).
- **`cli/classify_pr.py`**: `GitHubPort.get_pull_request_info(pr_number)`
  (from `--pr-number` CLI arg) -> for each `.changed_paths` entry matching
  a root path shape, `get_file_contents(path, info.base_sha)` and
  `get_file_contents(path, info.head_sha)`, parse each (missing base file ->
  `None`, matching `diff.classify_change`'s `before: PackageRoot | None`) ->
  `diff.classify_change` -> the **worst** classification across all changed
  roots wins (`"human-review-required"` > `"new-package"` > `"refresh"` —
  a PR touching two packages where one is a refresh and one needs human
  review is human-review-required overall) -> `add_labels`.
- **`cli/governance_check.py`** (extended, fork-PR announce revamp — G-19/
  G-20): re-derives the classification via
  `classify_pr.classify_pull_request` (unchanged single-source-of-truth
  approach), then: **machine lane** (`refresh`) requires the PR author's
  `github_id` (`PullRequestInfo.author_id`, §3) to appear in `owners[]` of
  *every* touched package root, read from the **base** ref only (never the
  PR head — the same `governance-gate` trust boundary
  `cli/classify_pr.py` already documents) — pass -> `success`; fail ->
  falls back to the human lane below. **Human lane** (`new-package`/
  `human-review-required`, or a refresh PR that failed G-19): always
  `pending` + reviewers assigned from committed `.github/maintainers.yml`
  (parsed via `core/maintainers.py::parse_maintainers`, read from the base
  ref) minus the PR author (self-review carve-out — GitHub's API itself
  rejects assigning a PR's own author as their own reviewer) via
  `GitHubPort.request_reviewers`, plus one idempotent comment via
  `GitHubPort.create_comment` (hidden HTML marker
  `<!-- indexbot:governance -->` — update-in-place on repeated runs, never
  reposted). Never `failure` — nothing has actually gone wrong, the PR just
  needs a human. (ADR-4 BD-5's fuller "green for refresh PRs once
  `schema-validate` is also green" cross-job condition remains deferred, per
  the original entry this replaces — unaffected by G-19/G-20.)

## 13. Consolidated open questions carried into Phase 2

1. **Yank-on-republish** (`core/regenerate.py`, §7): does a re-published
   digest under a yanked tag name clear the yank? Default: no. Confirm
   before Phase 3.
2. **Silent tag disappearance** — **resolved, ADR-6 FP-2/FP-3 (fork-PR
   announce revamp, 2026-07-18)**: a tag vanishing from the registry
   (`core/verify_claims.py`'s `"tag-missing-upstream"` finding) escalates to
   an anomaly in `cli/reconcile.py`'s verify-only sweep *unless* the
   committed `TagEntry.yanked is not None` for that tag — yank is grace, an
   explicit owner-authorized exemption; everything else vanished-upstream is
   an anomaly, never a silent drop. No longer decided by `core/regenerate.py`
   (which never runs in the verify-only sweep at all any more, §12).
3. **Pinned-vs-floating anomaly predicate** (`core/anomaly.py`, §7): ADR-4
   delegates this to ADR-1, which does not actually state it. Default: exact
   `X.Y.Z` only is pinned. This is the highest-stakes open question in this
   document — a wrong default either misses tamper or false-positives on
   routine cascade pushes. ADR-4's own Validation checklist already flags
   it; treat as blocking before Phase 3's E2E gate, not before Phase 2 build
   (Phase 2 can build and test against the stated default).
4. **Issue-creation on `GitHubPort`** (`cli/reconcile.py`, §12) — **resolved,
   fork-PR announce revamp 2026-07-18**: `create_or_update_issue` promoted
   onto `ports.GitHubPort` (§3/§10), implemented in `GitHubApi` (unchanged
   body — it already existed as an adapter-only capability) and `FakeGitHub`.
   `cli/reconcile.py`'s verify-only sweep now calls it directly on a
   non-empty escalating-finding set, before raising `AnomalyError`.
5. **`mirror.yml` parsing** (`cli/seed_import.py`, §12): implies a YAML
   dependency not currently declared; this stage may not edit
   `pyproject.toml`.
6. **`governance-check`'s cross-job read of `schema-validate`'s result**
   (`cli/governance_check.py`, §12): default proposal is a workflow-level
   `needs:`/`if:` gate rather than an API poll from inside the CLI; confirm
   this is sufficient when WP2-S designs `validate.yml`'s actual job graph.

## 14. Root serializer — client-facing byte-exact spec

Added fork-PR announce revamp, 2026-07-18, to give the byte-exact discipline
(§12's `cli/validate.py` entry) a single, precise, standalone reference —
this is the spec **ocx#216** (the client-side port of this same
serialization, for a publisher tool implemented outside this repo) ports
against. Restates §5/§1 in one place rather than requiring a cross-reader to
reassemble it from two sections; **not** a new rule — `validate_entry.py`'s
`serialize_package_root`/`serialize_observation_object` remain the one
authoritative implementation, this section documents their exact output byte
-for-byte.

**`p/<namespace>/<package>.json` (the package root — human-diffable, PR-review
form, never digested itself):**

- UTF-8 encoded, `ensure_ascii=True` (the `json.dumps` default, not passed
  explicitly but never overridden either) — non-ASCII field values (e.g. a
  non-ASCII `deprecated_message`, `desc.title`/`.description`, or
  `upstream.disclaimer`) serialize as `\uXXXX` escapes, never raw UTF-8
  bytes, identical to the observation-object form below. A `serde_json`
  (or any other) port that defaults to `to_string_pretty`'s
  UTF-8-passthrough behavior instead will byte-diverge on the first
  non-ASCII value it serializes — required reading for **ocx#216**.
- `json.dumps(data, indent=2, sort_keys=False)` — **2-space indent**,
  **insertion-order preserved** (never alphabetized).
- Key order is fixed and matches `model.PackageRoot`'s declared field order
  exactly: `name`, `repository`, `owners`, `status`, `deprecated_message`,
  `created`, `desc`, `upstream` (omitted entirely when `None` — schema
  forbids `null` there), `superseded_by` (omitted entirely when `None`,
  identical omit-when-absent contract), `tags`. Nested objects
  (`owners[]`, `desc`, `tags[*]`, `tags[*].yanked`) use their own dataclass's
  declared field order the same way — see `validate_entry.py`'s
  `_*_to_dict` helpers for the exact per-type key list.
- `desc: None` serializes as the JSON literal `null` (the key itself is
  **never** omitted — this is the one field whose absence-vs-null semantics
  differ from `upstream`/`superseded_by` above).
- `upstream`'s own three fields are **mixed** optional semantics, not a
  single rule applied uniformly: `org` is always present (required,
  non-nullable); `repository_url` is **omitted entirely** when `None`
  (schema forbids `null` there, matching `upstream` itself); `disclaimer`
  is **always present**, serialized as the JSON literal `null` when `None`
  (schema allows `null` there — the one sub-field of `upstream` that
  behaves like `desc` above rather than like `repository_url`).
- A single trailing `\n` — **always** present, exactly one byte, no more.
- Byte-exact discipline (this revamp): CI re-derives this exact form from
  the PR's own parsed root (`parse_package_root` -> `serialize_package_root`)
  and byte-compares against the committed bytes. A root that parses
  correctly but isn't already in this exact canonical form (different key
  order, different indent, minified, missing/extra trailing newline, ...)
  is rejected — `cli/validate.py`'s "committed bytes are not the canonical
  root serialization" failure. A publisher's own tooling (`cli/announce.py`,
  or a third-party port of this spec) must emit exactly this form, not
  merely schema-equivalent JSON.

**`p/<namespace>/<package>/o/sha256/<hex>.json` (the content-addressed
observation object — §1's canonical minified form, the one that's actually
digested):**

- UTF-8 encoded, `ensure_ascii=True` — non-ASCII field values (e.g. a
  variant string) serialize as `\uXXXX` escapes, never raw UTF-8 bytes; this
  is dedup-load-bearing (ADR-1 D4), not a stylistic choice.
- `json.dumps(data, sort_keys=True, separators=(",", ":"))` — **minified**
  (no whitespace at all between tokens), keys **alphabetized** (the opposite
  of the root's insertion-order rule above).
- `platforms[]` sorted by `platform_sort_key` — the tuple
  `(architecture, os, os_version or "", variant or "", os_features, features)`,
  compared as tuples element-by-element, **never** `",".join(...)`-ed into a
  string first (§1's own aliasing-prevention rationale). Registry
  manifest-list order must never leak into this digest.
- No trailing newline (unlike the root form above) — the digest is computed
  over these exact bytes with nothing appended.
- Desc blobs (`o/sha256/<hex>.md` — readme, `o/sha256/<hex>.{svg,png}` —
  logo) are **not** re-serialized at all: copied verbatim from the physical
  registry's `__ocx.desc` artifact layers, digest = `sha256` of those exact
  bytes. This spec's byte-exact rules above apply only to the two JSON forms
  (root, observation object); a desc blob's "canonical form" is simply
  whatever bytes the registry served.
