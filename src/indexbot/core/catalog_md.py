"""Per-package wrapper Markdown for the VitePress catalog build (ADR-3).

`render_wrapper_page` is `core/render.py`'s (WP2-F) wrapper-page emission
input — VitePress compiles `site/src/<namespace>/<package>.md` from this
function's output. Frontmatter degrades gracefully to the package's logical
name / empty description / empty keywords when `root.desc is None` (plan
Risk 6); the body links the CAS readme URL rather than inlining its content
(ADR-3's CAS-reference-not-duplication design). The logo is deliberately
**not** referenced here: a CAS digest alone does not carry its file
extension (`.svg` vs `.png`), which is only known from the actual CAS
filename — `core/render.py`'s `/data/catalog` emission resolves that (it has
`content_by_digest` to consult), this module does not (CONTRACTS.md §8).
"""

from __future__ import annotations

import json

from indexbot.model import PackageRoot


def cas_relpath(namespace: str, package: str, digest: str, ext: str) -> str:
    """Deployed relative path (no leading `/`) of a CAS object.

    `p/<namespace>/<package>/o/sha256/<hex>.<ext>` per the wire path map
    (`plan_index_v1.md`). `digest` is the full `sha256:<hex>` string; only
    the hex half appears in the path itself.
    """
    hex_digest = digest.removeprefix("sha256:")
    return f"p/{namespace}/{package}/o/sha256/{hex_digest}.{ext}"


def _namespace_and_package(root: PackageRoot) -> tuple[str, str]:
    """`root.name` (`ocx.sh/<namespace>/<package>`) -> `(namespace, package)`.

    `render_wrapper_page`'s signature is fixed to a single `root` argument
    (CONTRACTS.md §8), so this is the only namespace/package source
    available to it — `root.name`'s shape is already validated upstream by
    `core/validate_entry.py`'s `check_name_matches_path` (G-02).
    """
    namespace, _, package = root.name.removeprefix("ocx.sh/").partition("/")
    return namespace, package


def render_wrapper_page(root: PackageRoot) -> str:
    """VitePress-flavored Markdown wrapper page for one package. Pure."""
    title = root.desc.title if root.desc is not None else root.name
    description = root.desc.description if root.desc is not None else ""
    keywords = list(root.desc.keywords) if root.desc is not None else []

    lines = [
        "---",
        f"title: {json.dumps(title)}",
        f"description: {json.dumps(description)}",
        f"keywords: {json.dumps(keywords)}",
        "---",
        "",
        f"# {title}",
        "",
    ]

    if root.desc is not None and root.desc.readme is not None:
        namespace, package = _namespace_and_package(root)
        readme_path = cas_relpath(namespace, package, root.desc.readme, "md")
        lines.append(f"[Full README](/{readme_path})")
        lines.append("")

    return "\n".join(lines)
