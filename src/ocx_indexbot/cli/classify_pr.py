"""`indexbot classify-pr` — the `governance-gate` job's diff classifier
(CONTRACTS.md §12; ADR-4 BD-5, G-04/G-05).

Reads the PR's changed-file list and diff *via the GitHub API only*
(`ForgePort.get_pull_request_info`) — this module never checks out the PR
head, matching `governance-gate`'s `pull_request_target` trust boundary
(`.github/workflows/governance.yml`'s own top-of-file commentary; ADR-4 BD-5).

`classify_pull_request` is exported (not just an internal helper of `run`)
because `cli/governance_check.py` needs the exact same worst-classification-
wins aggregate to decide its own commit-status disposition, and
`governance.yml` invokes `indexbot governance-check` as a *separate* process
from `indexbot classify-pr` (no shared in-memory state, and no
`ForgePort.get_labels`-shaped method exists on `ports.ForgePort` to read
`classify-pr`'s label back) — re-deriving via the same pure aggregation
function is the boring, single-source-of-truth option (CONTRACTS.md §13 item
6's open question), not a second hand-rolled copy of G-04/G-05's diff logic.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, cast, get_args

from ocx_indexbot.core.diff import ChangeClass, classify_change
from ocx_indexbot.core.grammar import package_id_max_length
from ocx_indexbot.core.policy import IndexPolicy
from ocx_indexbot.core.validate_entry import parse_package_root
from ocx_indexbot.exit_codes import ExitCode

from ._common import write_ci_output

if TYPE_CHECKING:
    import argparse

    from ocx_indexbot.model import PullRequestInfo
    from ocx_indexbot.ports import ForgePort

_SEVERITY: Final[dict[ChangeClass, int]] = {
    "refresh": 0,
    "new-package": 1,
    "human-review-required": 2,
}
"""Worst-wins ordering (CONTRACTS.md §12): a PR touching two package roots,
one refresh-class and one new-package-class, classifies as `new-package`
overall — the most conservative disposition among every changed root wins."""


def _cas_path_max_length(name_segments: int) -> int:
    """`p/` + the package id + `/o/sha256/` + 64 hex + `.` + a 4-char
    extension. Derived from the declared depth rather than fixed at the
    two-segment 256, so BD-4's cap-before-regex order still has something to
    cap against on an index that nests deeper."""
    return len("p/") + package_id_max_length(name_segments) + len("/o/sha256/") + 64 + 1 + 4


_LEGACY_CAS_PATH_MAX_LENGTH: Final[int] = 256
"""The two-segment value this module used before 0.2.0 made depth
configuration: 221 legitimate characters, rounded up. Kept only as the
documented reference point for `_cas_path_max_length` above."""

_CAS_HEX_RE: Final[re.Pattern[str]] = re.compile(r"[a-f0-9]{64}")

_CAS_EXTENSIONS: Final[frozenset[str]] = frozenset({"json", "md", "svg", "png"})
"""Every extension `cli/announce.py` writes under a package's `o/sha256/`
tree: `.json` image indices plus the `.md` readme and `.svg`/`.png`
logo desc blobs (`announce._cas_path`/`_logo_extension`; CONTRACTS.md §7
`core/desc.py`, ADR-6 FP-4)."""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `classify-pr`'s CLI surface — `--pr-number` is
    a trusted GitHub Actions expression value (`github.event.pull_request.number`,
    `.github/workflows/governance.yml`'s `governance-gate` job), not an
    untrusted `client_payload` field, so no `cli/_common.read_validated_env`
    regex/length-cap discipline applies here (ADR-4 BD-4 scopes that to
    `repository_dispatch` payloads only)."""
    parser.add_argument(
        "--pr-number", type=int, required=True, help="pull request number to classify"
    )


def _is_package_root_path(path: str, *, name_segments: int) -> bool:
    """True iff `path` is a `p/<segments>.json` root path —
    excludes CAS objects (`p/<ns>/<pkg>/o/sha256/<hex>.json`, one level
    deeper) and anything outside `p/` entirely. Mirrors the shape check
    `cli/validate.py`'s `_package_id_from_root_path` and
    `cli/reconcile.py`'s `_discover_package_ids` each already hand-roll for
    their own call site (CONTRACTS.md's established per-module convention,
    not extracted into a shared helper here either)."""
    parts = path.split("/")
    return len(parts) == name_segments + 1 and parts[0] == "p" and parts[-1].endswith(".json")


def _cas_owner_root_path(path: str, *, name_segments: int) -> str | None:
    """The `p/<segments>.json` root that owns `path`, if `path` is a
    `p/<segments>/o/sha256/<64-hex>.<ext>` package-local CAS object — else
    `None`.

    Hand-rolled shape check, same per-module convention as
    `_is_package_root_path` above. Total by construction: a malformed
    CAS-shaped path returns `None` rather than raising, so an unparseable
    diff entry lands on the caller's conservative branch instead of crashing
    the classifier.
    """
    if len(path) > _cas_path_max_length(name_segments):
        return None
    parts = path.split("/")
    if (
        len(parts) != name_segments + 4
        or parts[0] != "p"
        or parts[name_segments + 1] != "o"
        or parts[name_segments + 2] != "sha256"
    ):
        return None
    hex_digest, _, extension = parts[-1].partition(".")
    if extension not in _CAS_EXTENSIONS or _CAS_HEX_RE.fullmatch(hex_digest) is None:
        return None
    return "p/" + "/".join(parts[1 : name_segments + 1]) + ".json"


def _every_path_in_refresh_scope(
    changed_paths: tuple[str, ...], root_paths: frozenset[str], *, name_segments: int
) -> bool:
    """True iff every changed path is one of `root_paths` itself or a CAS
    object belonging to one of those exact packages — i.e. the diff contains
    nothing beyond what `cli/announce.py` writes for those roots (its
    `files_by_path`: the root, the tags' image indices, the readme/logo
    desc blobs).

    A CAS path under a package whose root is *not* in `root_paths` is out of
    scope — package-local CAS is only in scope alongside its own root.
    """
    return all(
        path in root_paths or _cas_owner_root_path(path, name_segments=name_segments) in root_paths
        for path in changed_paths
    )


def _classify_one_root(github: ForgePort, path: str, info: PullRequestInfo) -> ChangeClass:
    base_raw = github.get_file_contents(path, info.base_sha)
    head_raw = github.get_file_contents(path, info.head_sha)
    if head_raw is None:
        # The root was deleted in this PR — `diff.classify_change`'s shape
        # (`after: PackageRoot`, never `None`) has no representation for
        # that. A package removal is always the most conservative outcome,
        # never auto-classified as a routine refresh.
        return "human-review-required"
    before = parse_package_root(base_raw) if base_raw is not None else None
    after = parse_package_root(head_raw)
    return classify_change(before, after)


def classify_pull_request(
    info: PullRequestInfo, github: ForgePort, *, policy: IndexPolicy
) -> ChangeClass:
    """Worst-classification-wins aggregate across every
    `p/<namespace>/<package>.json` root in `info.changed_paths`
    (CONTRACTS.md §12).

    A PR touching zero package roots (e.g. a workflow- or docs-only change)
    is conservatively `"human-review-required"` — the indexbot automation
    lane exists for registry-truth refreshes, never for auto-merging a PR
    that happens not to touch any `p/**` root.

    The root filter selects which files get *classified*; it does not decide
    which files are *allowed*. Any changed path outside the refresh scope of
    the roots it selected (a workflow edit, `bot/**` source, another
    package's files, an unrelated deletion) is `"human-review-required"` on
    its own — ADR-6 FP-5: machine-lane content consists only of authorized
    package refreshes. Ignoring the rest of the diff instead would let an
    owner of one package attach arbitrary repository content to a
    refresh-classified PR and ride `governance.yml`'s `gh pr merge --auto`.
    """
    root_paths = [
        path
        for path in info.changed_paths
        if _is_package_root_path(path, name_segments=policy.name_segments)
    ]
    if not root_paths:
        return "human-review-required"
    if not _every_path_in_refresh_scope(
        info.changed_paths, frozenset(root_paths), name_segments=policy.name_segments
    ):
        return "human-review-required"
    worst: ChangeClass = "refresh"
    for path in root_paths:
        change_class = _classify_one_root(github, path, info)
        if _SEVERITY[change_class] > _SEVERITY[worst]:
            worst = change_class
    return worst


def run(args: argparse.Namespace, *, github: ForgePort, policy: IndexPolicy) -> ExitCode:
    """`indexbot classify-pr --pr-number <n>` entry point. See module
    docstring for the pipeline; `classify_pull_request` is this module's
    reusable core, `cli/governance_check.py`'s only import from here."""
    pr_number = cast(int, args.pr_number)
    info = github.get_pull_request_info(pr_number)
    classification = classify_pull_request(info, github, policy=policy)

    apply_change_class(info, classification, github)
    write_ci_output("classification", classification)
    return ExitCode.OK


def apply_change_class(info: PullRequestInfo, change_class: ChangeClass, github: ForgePort) -> None:
    """Make the PR's lane labels say exactly one thing: `change_class`.

    `add_labels` merges, so a pull request reclassified between sweeps used to
    keep the label its *previous* head earned. A merge request that arrived as
    `human-review-required`, was corrected, and then merged as `refresh` ended
    up carrying both — reading, to anyone auditing the repository later, as
    automation that merged something a human was required to look at.

    Nothing in the automation is misled by that: no `ForgePort.get_labels`
    exists and every consumer re-derives the classification from the diff (see
    this module's docstring). The labels are the *human's* record, and that is
    exactly why a stale one is worth removing — a record only a human reads is
    a record only a human can be misled by.

    The removals come from `info.labels`, which
    `ForgePort.get_pull_request_info` already carries for `indexbot stale`, so
    this costs no extra round-trip on the common path where the class did not
    change. Removing only what is actually there also keeps a deployment that
    never had these labels from making three no-op writes per sweep.
    """
    github.add_labels(info.number, [change_class])
    for stale in get_args(ChangeClass):
        if stale != change_class and stale in info.labels:
            github.remove_label(info.number, stale)
