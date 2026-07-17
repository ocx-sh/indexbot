"""`indexbot seed-import` — bootstrap a brand-new package root from local seed files.

Distinct from `cli/announce.py`'s regenerate-an-existing-root flow: this module
*synthesizes* a `PackageRoot` from scratch — the one place in the bot allowed to
do so (`core/regenerate.py` explicitly refuses to, per CONTRACTS.md §7 — a
package_id with no committed root is a namespace-claiming concern, not
`regenerate`'s). Refuses to run if the target root already exists (`FilePort`
check) rather than silently overwriting a governed, human-owned root.

Inputs, all read via `FilePort` (never a bare filesystem call):

- `--catalog-md`: a local Markdown file with a `---`-delimited frontmatter block
  (`title`, `description`, `keywords` — comma-separated, matching the
  `sh.ocx.keywords` convention `core/desc.py` reads from the registry) followed
  by the package's readme body. Frontmatter shape is this module's own design
  choice — CONTRACTS.md flagged it as "TBD by whoever writes this WP"; see
  `open_questions`.
- `--logo`: optional `.svg`/`.png` file, copied verbatim into this package's CAS.
- `--mirror-yml`: a minimal hand-rolled flat `key: value` reader (no YAML
  dependency declared in `pyproject.toml`, CONTRACTS.md §12/§13 open question 5)
  for the one key this module needs: `repository`.
- `--namespace`/`--package`, or derived from `--catalog-md`'s path (its parent
  two path segments, e.g. `seeds/kitware/cmake/CATALOG.md` -> `kitware/cmake`).
- `--owner-github`/`--owner-github-id` and optional `--upstream-*`: **not** in
  CONTRACTS.md §12's args list for this module, but `PackageRoot.owners` is
  schema-required (`minItems: 1`, `github_id` mandatory per ADR-2 ND-8) and
  neither CATALOG.md nor mirror.yml carries ownership/attribution data — see
  `open_questions`.

Desc content (title/description/keywords/readme/logo) comes from these local
seed files, not from the physical registry's `__ocx.desc` tag — that artifact
only exists once the package has actually been mirrored and separately
publishes its own description (`core/desc.py`'s concern, exercised by
`announce`/`reconcile`, not here). See `open_questions` for the resulting
`desc.digest` gap this creates for a package that never publishes `__ocx.desc`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, cast

from indexbot.core.observe import observe
from indexbot.core.validate_entry import (
    check_namespace_not_reserved,
    check_repository_allowlisted,
    check_repository_shape,
    parse_digest,
    serialize_observation_object,
    serialize_package_root,
)
from indexbot.core.validate_payload import parse_package_id
from indexbot.errors import ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import Desc, Owner, PackageRoot, TagEntry, Upstream

if TYPE_CHECKING:
    from indexbot.ports import ClockPort, FilePort, RegistryPort

_FRONTMATTER_DELIMITER: Final[str] = "---"
_LOGO_EXTENSIONS: Final[dict[str, str]] = {".svg": "svg", ".png": "png"}
_DEFAULT_OUT_DIR: Final[str] = "p"


@dataclass(frozen=True, slots=True)
class _Frontmatter:
    """Parsed `--catalog-md` content: frontmatter fields plus the readme body."""

    title: str
    description: str
    keywords: tuple[str, ...]
    body: str


def _parse_catalog_md(raw: str, *, source: str) -> _Frontmatter:
    """`---`-delimited frontmatter (`title`, `description`, `keywords`) plus body.

    Raises `ValidationError` on any structurally malformed input — missing
    opening/closing delimiter, a frontmatter line with no `:`, or a missing
    required `title`/`description` field. `keywords` is optional, comma-separated
    (matching `core/desc.py`'s `sh.ocx.keywords` convention), defaulting to `()`.
    """
    lines = raw.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        raise ValidationError(f"{source}: missing frontmatter (must start with '---')")
    try:
        closing = lines.index(_FRONTMATTER_DELIMITER, 1)
    except ValueError as exc:
        raise ValidationError(f"{source}: frontmatter block never closes with '---'") from exc

    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        stripped = line.strip()
        if not stripped:
            continue
        if ":" not in stripped:
            raise ValidationError(f"{source}: malformed frontmatter line {line!r}")
        key, _, value = stripped.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")

    title = fields.get("title", "")
    if not title:
        raise ValidationError(f"{source}: frontmatter missing required 'title'")
    description = fields.get("description", "")
    if not description:
        raise ValidationError(f"{source}: frontmatter missing required 'description'")
    keywords_raw = fields.get("keywords", "")
    keywords = tuple(part.strip() for part in keywords_raw.split(",") if part.strip())

    body = "\n".join(lines[closing + 1 :]).lstrip("\n")
    return _Frontmatter(title=title, description=description, keywords=keywords, body=body)


def _parse_mirror_yml(raw: str, *, source: str) -> dict[str, str]:
    """Minimal hand-rolled flat `key: value` reader for `mirror.yml`.

    ponytail: no YAML dependency declared in `pyproject.toml` (open_questions,
    CONTRACTS.md §13 item 5) — every known `mirror.yml` shape is a flat mapping
    (this module only ever reads its `repository` key), so a real YAML parser
    would be premature. Upgrade to `pyyaml`/`ruamel.yaml` (pyproject change, a
    separate reviewed PR) if a future seed's `mirror.yml` grows nesting or lists.
    """
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValidationError(f"{source}: malformed line {line!r}")
        key, _, value = stripped.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def _derive_package_id(catalog_md_path: str) -> tuple[str, str]:
    """`<namespace>/<package>` from `--catalog-md`'s parent two path segments.

    ponytail: assumes a `.../<namespace>/<package>/CATALOG.md` seed layout —
    Phase 4's actual seed directory convention is not yet designed (plan
    Phase 4 is future work). Confirm against real seed data; pass
    `--namespace`/`--package` explicitly to bypass this entirely.
    """
    parts = [p for p in catalog_md_path.split("/") if p]
    if len(parts) < 3:
        raise ValidationError(
            f"cannot derive namespace/package from {catalog_md_path!r} "
            "(expected .../<namespace>/<package>/CATALOG.md); pass --namespace/--package"
        )
    return parts[-3], parts[-2]


def _logo_extension(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    try:
        return _LOGO_EXTENSIONS[suffix]
    except KeyError:
        raise ValidationError(
            f"unsupported logo extension {suffix!r} (want .svg or .png)"
        ) from None


def _cas_path(package_dir: str, digest: str, ext: str) -> str:
    """`<package_dir>/o/sha256/<hex>.<ext>` — digest-hex `fullmatch` before path join."""
    hex_part = parse_digest(digest).removeprefix("sha256:")
    return f"{package_dir}/o/sha256/{hex_part}.{ext}"


def _seed_desc_digest(title: str, description: str, keywords: tuple[str, ...]) -> str:
    """Locally computed `desc.digest` placeholder for a package that has never
    published a physical `__ocx.desc` registry tag.

    `schema/root.schema.json` requires `desc.digest` to `fullmatch`
    `sha256:[a-f0-9]{64}` regardless of provenance, so seed-import cannot leave
    it blank or use a distinguishing prefix — this hashes the seeded
    title/description/keywords canonically (§1 form) instead.

    **Open question, not silently resolved**: `core/desc.py`'s
    `check_desc_change` compares `registry.get_desc_tag_digest(repository)`
    (`None` until the package ever publishes `__ocx.desc`) against
    `current.digest`. If it never publishes one, every future `announce` run
    sees `observed_digest=None != current_digest=<this placeholder>` and hits
    `check_desc_change`'s "tag disappeared" branch (`ValueError`) rather than
    "no `__ocx.desc` published yet, keep the seeded desc" — a real gap in the
    seed-import -> announce handoff that this function's placeholder does not
    fix, only makes the seeded root schema-valid at commit time. Confirm with
    the owner before Phase 3 whether `core/desc.py` needs a third state.
    """
    canonical = json.dumps(
        {"title": title, "description": description, "keywords": list(keywords)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def run(
    args: argparse.Namespace,
    *,
    registry: RegistryPort,
    files: FilePort,
    clock: ClockPort,
) -> ExitCode:
    """Import one brand-new package from local seed files plus a live registry observe.

    Expected `args` attributes: `catalog_md` (str), `mirror_yml` (str), `logo`
    (str | None), `namespace` (str | None), `package` (str | None), `out` (str,
    defaults to `"p"`), `owner_github` (str), `owner_github_id` (int | str),
    `upstream_org`/`upstream_repository_url`/`upstream_disclaimer` (str | None).
    `argparse.Namespace` wiring (WP2-M) is expected to supply all of these; this
    signature's `registry`/`files`/`clock` keyword-only ports are this module's
    own addition on top of CONTRACTS.md §12's literal `run(args) -> ExitCode`
    shape — flagged in `open_questions` since WP2-M's `_DISPATCH` wiring will
    need to adapt (bind real adapters via a closure/`functools.partial`) rather
    than call this signature directly as `Callable[[argparse.Namespace], ExitCode]`.
    """
    catalog_md_path = cast(str, args.catalog_md)
    mirror_yml_path = cast(str, args.mirror_yml)
    logo_path = cast("str | None", getattr(args, "logo", None))
    out_dir = cast(str, getattr(args, "out", None) or _DEFAULT_OUT_DIR)
    owner_github = cast(str, args.owner_github)
    owner_github_id = int(cast("str | int", args.owner_github_id))
    upstream_org = cast("str | None", getattr(args, "upstream_org", None))
    upstream_repository_url = cast("str | None", getattr(args, "upstream_repository_url", None))
    upstream_disclaimer = cast("str | None", getattr(args, "upstream_disclaimer", None))

    namespace = cast("str | None", getattr(args, "namespace", None))
    package = cast("str | None", getattr(args, "package", None))
    if bool(namespace) != bool(package):
        raise ValidationError("--namespace and --package must be given together, or neither")
    if not namespace or not package:
        namespace, package = _derive_package_id(catalog_md_path)
    package_id = parse_package_id(f"{namespace}/{package}")
    check_namespace_not_reserved(package_id)

    package_dir = f"{out_dir}/{package_id.namespace}/{package_id.package}"
    root_path = f"{package_dir}.json"
    if files.exists(root_path):
        raise ValidationError(
            f"{root_path} already exists; seed-import only creates brand-new packages "
            "(use announce to refresh an existing one)"
        )

    catalog_raw = files.read_text(catalog_md_path)
    if catalog_raw is None:
        raise ValidationError(f"{catalog_md_path} does not exist")
    frontmatter = _parse_catalog_md(catalog_raw, source=catalog_md_path)

    mirror_raw = files.read_text(mirror_yml_path)
    if mirror_raw is None:
        raise ValidationError(f"{mirror_yml_path} does not exist")
    repository = _parse_mirror_yml(mirror_raw, source=mirror_yml_path).get("repository")
    if not repository:
        raise ValidationError(f"{mirror_yml_path}: missing required 'repository' key")

    # G-03/SSRF ordering: both checks are pure string parsing (no RegistryPort
    # call inside either) and must run before `observe()` below.
    check_repository_allowlisted(repository)
    check_repository_shape(repository)

    # (bytes, digest, extension) as one unit — never tracked as three separately
    # nullable variables that could drift out of sync (and would otherwise force
    # an unreachable partial-None branch in the write step below).
    logo: tuple[bytes, str, str] | None = None
    if logo_path:
        logo_bytes = files.read_bytes(logo_path)
        if logo_bytes is None:
            raise ValidationError(f"{logo_path} does not exist")
        logo_ext = _logo_extension(logo_path)
        logo = (logo_bytes, f"sha256:{hashlib.sha256(logo_bytes).hexdigest()}", logo_ext)

    observations = observe(repository, registry)
    if not observations:
        raise ValidationError(f"{repository!r} has no observable tags; nothing to seed")

    readme_bytes = frontmatter.body.encode("utf-8")
    readme_digest = f"sha256:{hashlib.sha256(readme_bytes).hexdigest()}"

    desc = Desc(
        digest=_seed_desc_digest(frontmatter.title, frontmatter.description, frontmatter.keywords),
        title=frontmatter.title,
        description=frontmatter.description,
        keywords=frontmatter.keywords,
        readme=readme_digest,
        logo=logo[1] if logo is not None else None,
    )

    tags = {
        observation.tag: TagEntry(content=observation.content_digest, observed=clock.now_iso8601())
        for observation in observations
    }

    upstream = (
        Upstream(
            org=upstream_org,
            repository_url=upstream_repository_url,
            disclaimer=upstream_disclaimer,
        )
        if upstream_org
        else None
    )

    root = PackageRoot(
        name=f"ocx.sh/{package_id.namespace}/{package_id.package}",
        repository=repository,
        owners=(Owner(github=owner_github, github_id=owner_github_id),),
        status="active",
        deprecated_message=None,
        created=clock.now_iso8601()[:10],
        desc=desc,
        upstream=upstream,
        tags=tags,
    )

    files.write_bytes(root_path, serialize_package_root(root))

    written_object_digests: set[str] = set()
    for observation in observations:
        if observation.content_digest in written_object_digests:
            continue
        written_object_digests.add(observation.content_digest)
        files.write_bytes(
            _cas_path(package_dir, observation.content_digest, "json"),
            serialize_observation_object(observation.object),
        )

    files.write_bytes(_cas_path(package_dir, readme_digest, "md"), readme_bytes)
    if logo is not None:
        logo_content, logo_digest, logo_ext = logo
        files.write_bytes(_cas_path(package_dir, logo_digest, logo_ext), logo_content)

    return ExitCode.OK
