"""`indexbot classify-pr` — the `governance-gate` job's diff classifier
(CONTRACTS.md §12; ADR-4 BD-5, G-04/G-05).

Reads the PR's changed-file list and diff *via the GitHub API only*
(`GitHubPort.get_pull_request_info`) — this module never checks out the PR
head, matching `governance-gate`'s `pull_request_target` trust boundary
(`.github/workflows/validate.yml`'s own top-of-file commentary; ADR-4 BD-5).

`classify_pull_request` is exported (not just an internal helper of `run`)
because `cli/governance_check.py` needs the exact same worst-classification-
wins aggregate to decide its own commit-status disposition, and
`validate.yml` invokes `indexbot governance-check` as a *separate* process
from `indexbot classify-pr` (no shared in-memory state, and no
`GitHubPort.get_labels`-shaped method exists on `ports.GitHubPort` to read
`classify-pr`'s label back) — re-deriving via the same pure aggregation
function is the boring, single-source-of-truth option (CONTRACTS.md §13 item
6's open question), not a second hand-rolled copy of G-04/G-05's diff logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

from indexbot.core.diff import ChangeClass, classify_change
from indexbot.core.validate_entry import parse_package_root
from indexbot.exit_codes import ExitCode

from ._common import write_github_output

if TYPE_CHECKING:
    import argparse

    from indexbot.model import PullRequestInfo
    from indexbot.ports import GitHubPort

_SEVERITY: Final[dict[ChangeClass, int]] = {
    "refresh": 0,
    "new-package": 1,
    "human-review-required": 2,
}
"""Worst-wins ordering (CONTRACTS.md §12): a PR touching two package roots,
one refresh-class and one new-package-class, classifies as `new-package`
overall — the most conservative disposition among every changed root wins."""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate `parser` with `classify-pr`'s CLI surface — `--pr-number` is
    a trusted GitHub Actions expression value (`github.event.pull_request.number`,
    `.github/workflows/validate.yml`'s `governance-gate` job), not an
    untrusted `client_payload` field, so no `cli/_common.read_validated_env`
    regex/length-cap discipline applies here (ADR-4 BD-4 scopes that to
    `repository_dispatch` payloads only)."""
    parser.add_argument(
        "--pr-number", type=int, required=True, help="pull request number to classify"
    )


def _is_package_root_path(path: str) -> bool:
    """True iff `path` is a `p/<namespace>/<package>.json` root path —
    excludes CAS objects (`p/<ns>/<pkg>/o/sha256/<hex>.json`, one level
    deeper) and anything outside `p/` entirely. Mirrors the shape check
    `cli/validate.py`'s `_package_id_from_root_path` and
    `cli/reconcile.py`'s `_discover_package_ids` each already hand-roll for
    their own call site (CONTRACTS.md's established per-module convention,
    not extracted into a shared helper here either)."""
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == "p" and parts[2].endswith(".json")


def _classify_one_root(github: GitHubPort, path: str, info: PullRequestInfo) -> ChangeClass:
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


def classify_pull_request(info: PullRequestInfo, github: GitHubPort) -> ChangeClass:
    """Worst-classification-wins aggregate across every
    `p/<namespace>/<package>.json` root in `info.changed_paths`
    (CONTRACTS.md §12).

    A PR touching zero package roots (e.g. a workflow- or docs-only change)
    is conservatively `"human-review-required"` — the indexbot automation
    lane exists for registry-truth refreshes, never for auto-merging a PR
    that happens not to touch any `p/**` root.
    """
    root_paths = [path for path in info.changed_paths if _is_package_root_path(path)]
    if not root_paths:
        return "human-review-required"
    worst: ChangeClass = "refresh"
    for path in root_paths:
        change_class = _classify_one_root(github, path, info)
        if _SEVERITY[change_class] > _SEVERITY[worst]:
            worst = change_class
    return worst


def run(args: argparse.Namespace, *, github: GitHubPort) -> ExitCode:
    """`indexbot classify-pr --pr-number <n>` entry point. See module
    docstring for the pipeline; `classify_pull_request` is this module's
    reusable core, `cli/governance_check.py`'s only import from here."""
    pr_number = cast(int, args.pr_number)
    info = github.get_pull_request_info(pr_number)
    classification = classify_pull_request(info, github)

    github.add_labels(pr_number, [classification])
    write_github_output("classification", classification)
    return ExitCode.OK
