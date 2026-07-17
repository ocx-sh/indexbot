"""`indexbot render` -- CONTRACTS.md §12/§8. Reads the committed `p/` source
tree via `FilePort` and executes `core/render.py`'s pure `build_render_plan`
against it: `--site-dist` writes `wrapper_pages` (VitePress compile input,
`site/src/**`, before the build); `--out` writes `dist_files` (`config.json`
+ the `/p/**` wire mirror + `/data/catalog/**`, after the build). Both are
independent -- `render-deploy.yml` (ADR-3, WP3-A) invokes `indexbot render`
twice with the VitePress build sandwiched in between, once per flag, so at
least one but not necessarily both is given per invocation; a call with
neither is a usage error.

`--check` computes the plan and reports drift against whichever tree(s) were
given -- a planned file missing or content-mismatched, or an existing file
under that tree the plan no longer produces (a stale orphan, e.g. a pruned
CAS blob) -- without writing anything. `ExitCode.VALIDATION_FAILURE` on
drift, `ExitCode.OK` when clean; lets CI catch a stale committed dist tree
without re-running the VitePress build (plan risk 3).

`files: FilePort` is a required keyword argument rather than constructed
inside this module (functional core / imperative shell, CONTRACTS.md §0) --
matching `cli/reconcile.py`/`cli/seed_import.py`'s established pattern. This
is this module's own addition on top of CONTRACTS.md §12's literal
`run(args) -> ExitCode` shape; WP2-M's `_DISPATCH` wiring needs to bind the
real `LocalFiles` adapter via a closure/`functools.partial` rather than call
this signature directly as `Callable[[argparse.Namespace], ExitCode]` --
flagged in `open_questions`, same theme as the two sibling modules'.

`--index-dir`/`--site-dist`/`--out` are relative path *prefixes* within the
one `files` root, not separate filesystem roots -- e.g. `index_dir=""` (`p/`
lives at the checkout root), `site_dist="site/src"`,
`out="site/.vitepress/dist"`, all reachable from one repo-checkout-rooted
`LocalFiles` in production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from indexbot.core.render import FileWrite, SourcePackage, build_render_plan
from indexbot.core.validate_entry import parse_package_root
from indexbot.errors import ValidationError
from indexbot.exit_codes import ExitCode
from indexbot.model import PackageId

if TYPE_CHECKING:
    import argparse

    from indexbot.ports import FilePort


def _p_prefix(index_dir: str) -> str:
    """`index_dir` (a `--index-dir` value, no trailing slash required)
    normalized to the `p/` listing prefix within `files`."""
    stripped = index_dir.rstrip("/")
    return f"{stripped}/p/" if stripped else "p/"


def _package_dir(index_dir: str, package_id: PackageId) -> str:
    return f"{_p_prefix(index_dir)}{package_id.namespace}/{package_id.package}"


def _output_prefix(output_root: str) -> str:
    """`output_root` (a `--site-dist`/`--out` value) normalized to a path
    prefix -- `""` (the `files` root itself) or `"<root>/"`."""
    stripped = output_root.rstrip("/")
    return f"{stripped}/" if stripped else ""


def _read_required_bytes(files: FilePort, path: str) -> bytes:
    """`files.read_bytes(path)`, raising `ValidationError` if it vanished
    between being listed (`FilePort.list_files`) and read -- a torn local
    checkout; the closest existing error class for "an input this call
    expected isn't there"."""
    content = files.read_bytes(path)
    if content is None:
        raise ValidationError(f"expected file vanished during render: {path!r}")
    return content


def _package_roots(files: FilePort, index_dir: str) -> list[tuple[PackageId, str]]:
    """`(PackageId, root_path)` for every `p/<namespace>/<package>.json` --
    exactly two path segments under the `p/` prefix ending in `.json`, which
    excludes every CAS subtree file (`p/<ns>/<pkg>/o/sha256/**`, always
    3+ segments). Mirrors `cli/reconcile.py`'s `_discover_package_ids`
    predicate verbatim -- see `open_questions` re: extracting a shared
    helper, out of this work package's scope to do unilaterally."""
    prefix = _p_prefix(index_dir)
    roots: list[tuple[PackageId, str]] = []
    for path in files.list_files(prefix):
        segments = path.removeprefix(prefix).split("/")
        if len(segments) == 2 and segments[1].endswith(".json"):
            namespace, filename = segments
            package_id = PackageId(namespace=namespace, package=filename.removesuffix(".json"))
            roots.append((package_id, path))
    return roots


def _load_source_package(
    files: FilePort, index_dir: str, package_id: PackageId, root_path: str
) -> SourcePackage:
    root_raw = _read_required_bytes(files, root_path)
    root = parse_package_root(root_raw)
    cas_prefix = f"{_package_dir(index_dir, package_id)}/o/sha256/"
    content_by_digest = {
        f"sha256:{cas_path.removeprefix(cas_prefix)}": _read_required_bytes(files, cas_path)
        for cas_path in files.list_files(cas_prefix)
    }
    return SourcePackage(
        package_id=package_id, root=root, root_raw=root_raw, content_by_digest=content_by_digest
    )


def _load_source_packages(files: FilePort, index_dir: str) -> list[SourcePackage]:
    return [
        _load_source_package(files, index_dir, package_id, root_path)
        for package_id, root_path in _package_roots(files, index_dir)
    ]


def _write_tree(files: FilePort, output_root: str, file_writes: tuple[FileWrite, ...]) -> None:
    prefix = _output_prefix(output_root)
    for file_write in file_writes:
        path = f"{prefix}{file_write.path}"
        if isinstance(file_write.content, str):
            files.write_text(path, file_write.content)
        else:
            files.write_bytes(path, file_write.content)


def _tree_drifted(files: FilePort, output_root: str, file_writes: tuple[FileWrite, ...]) -> bool:
    prefix = _output_prefix(output_root)
    planned = {f"{prefix}{file_write.path}": file_write for file_write in file_writes}
    if set(files.list_files(output_root.rstrip("/"))) != set(planned):
        return True
    for path, file_write in planned.items():
        content = file_write.content
        expected = content.encode("utf-8") if isinstance(content, str) else content
        if files.read_bytes(path) != expected:
            return True
    return False


def run(args: argparse.Namespace, *, files: FilePort) -> ExitCode:
    """`indexbot render` entry point. Expected `args` attributes:
    `index_dir` (str, required), `site_dist` (str | None), `out`
    (str | None), `check` (bool) -- argparse's default dest derivation
    already maps `--index-dir`/`--site-dist`/`--out`/`--check` to those
    names, so WP2-M's `subparsers.add_parser` wiring needs no extra `dest=`
    overrides.
    """
    index_dir = cast(str, args.index_dir)
    site_dist = cast("str | None", getattr(args, "site_dist", None))
    out = cast("str | None", getattr(args, "out", None))
    check = bool(getattr(args, "check", False))

    if site_dist is None and out is None:
        raise ValidationError("render requires at least one of --site-dist or --out")

    plan = build_render_plan(_load_source_packages(files, index_dir))

    if check:
        site_drifted = site_dist is not None and _tree_drifted(files, site_dist, plan.wrapper_pages)
        dist_drifted = out is not None and _tree_drifted(files, out, plan.dist_files)
        return ExitCode.VALIDATION_FAILURE if site_drifted or dist_drifted else ExitCode.OK

    if site_dist is not None:
        _write_tree(files, site_dist, plan.wrapper_pages)
    if out is not None:
        _write_tree(files, out, plan.dist_files)
    return ExitCode.OK
