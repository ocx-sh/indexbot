"""`validate.yml`'s changed-files pathspec selects package roots, nothing else.

The `schema-validate-pr` job hands `indexbot validate` the
`p/<ns>/<pkg>.json` roots a PR touches. A git pathspec is NOT a shell glob:
its `*` matches `/` too unless `:(glob)` magic switches on FNM_PATHNAME. So
the bare `p/*/*.json` this step used to carry also selected every
`p/<ns>/<pkg>/o/sha256/<hex>.json` CAS object the PR added — and validate
rejects a CAS object as a malformed root. Every announce PR adds a CAS
object, so the required check failed on every announce PR (ocx-sh/index#57);
`p/` being empty is the only reason it stayed invisible until the first real
announce.

Unlike `test_workflow_split.py`'s stdlib line scans, this module runs the
pathspec the workflow actually carries against real `git` on a real fixture
tree. A static regex would pin the spelling of the fix without proving what
it selects — and the whole bug was that the spelling and the selection
disagreed.

Coverage source is `src/` only (`pyproject.toml [tool.coverage.run]`), so
nothing here moves the 100% branch-coverage gate.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_REPO_ROOT = Path(__file__).resolve().parents[3]
_VALIDATE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "validate.yml"

_CHANGED_FILES_DIFF_RE = re.compile(
    r"""git diff --name-only --diff-filter=d "(\$BASE_SHA\.\.\.HEAD)" -- '([^']+)'"""
)
_BARE_PATHSPEC = "p/*/*.json"
"""The broken shell-glob spelling, kept as the regression witness below."""

_PACKAGE_ROOTS = (
    "p/michael-herwig/ocx-e2e-hello.json",
    # The full schema-legal package charset (`[a-z0-9]` plus `.`, `_`, `-`):
    # none of it is glob-magic, so `:(glob)` costs no expressiveness here.
    "p/ns/pkg.name_x-y.json",
)
_NOT_PACKAGE_ROOTS = (
    # The CAS object every announce PR adds — the exact file that broke #57.
    "p/michael-herwig/ocx-e2e-hello/o/sha256/"
    "4ee19e66016380ec603fee8f6b8d8fda85768d779944a942f3d42befe451fe91.json",
    "p/ns/deeper/still/pkg.json",
    "p/toplevel.json",
    "p/ns/pkg.txt",
    "schema/fixtures/valid/root-valid.json",
)

_GIT_ENV = {
    # Hermetic: a developer's global/system git config (hooks, gpgsign,
    # excludesFile, init.defaultBranch) must not reach this fixture repo.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "indexbot-tester",
    "GIT_AUTHOR_EMAIL": "indexbot-tester@example.invalid",
    "GIT_COMMITTER_NAME": "indexbot-tester",
    "GIT_COMMITTER_EMAIL": "indexbot-tester@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no untrusted input
        ["git", *args],  # noqa: S607 - `git` off the test runner's own PATH, as CI resolves it
        cwd=repo,
        env=os.environ | _GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _workflow_diff() -> tuple[str, str]:
    """The range template and pathspec literal `validate.yml`'s changed-files
    step runs. Capturing the range and EXECUTING it below is what binds the
    three-dot spelling to behavior — a static regex alone would pin the
    spelling without proving what it selects, this module's founding sin."""
    matches = _CHANGED_FILES_DIFF_RE.findall(_VALIDATE_WORKFLOW.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"expected exactly one changed-files git diff, found {matches}"
    range_template, pathspec = matches[0]
    return str(range_template), str(pathspec)


def _workflow_pathspec() -> str:
    """The single pathspec literal `validate.yml`'s changed-files step runs."""
    return _workflow_diff()[1]


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    """An index-shaped two-commit repo: an empty base, then one commit adding
    every path in `_PACKAGE_ROOTS` + `_NOT_PACKAGE_ROOTS`. Returns the repo
    and its base SHA — the two `git diff` endpoints the workflow uses."""
    repo = tmp_path / "index"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    for relpath in (*_PACKAGE_ROOTS, *_NOT_PACKAGE_ROOTS):
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "announce")

    return repo, base_sha


def _changed(repo: Path, base_sha: str, pathspec: str) -> Sequence[str]:
    """Run the diff exactly as the workflow spells it — the captured range
    template with `$BASE_SHA` substituted, the captured `--diff-filter=d`."""
    range_template, _ = _workflow_diff()
    range_arg = range_template.replace("$BASE_SHA", base_sha)
    stdout = _git(repo, "diff", "--name-only", "--diff-filter=d", range_arg, "--", pathspec)
    return stdout.split()


def test_changed_files_pathspec_selects_package_roots_only(tmp_path: Path) -> None:
    """The pathspec `validate.yml` actually carries selects exactly the
    `p/<ns>/<pkg>.json` roots — no CAS object, nothing deeper, nothing
    outside `p/`."""
    repo, base_sha = _fixture_repo(tmp_path)

    selected = _changed(repo, base_sha, _workflow_pathspec())

    assert sorted(selected) == sorted(_PACKAGE_ROOTS)


def test_bare_shell_glob_pathspec_over_selects_cas_objects(tmp_path: Path) -> None:
    """Regression witness for ocx-sh/index#57, two halves.

    First: the bare shell-glob spelling really does drag CAS objects (and
    every deeper path) into the selection — proving the fixture above
    discriminates, so the passing test cannot quietly stop meaning anything.
    Second: `validate.yml` does not carry that spelling.
    """
    repo, base_sha = _fixture_repo(tmp_path)

    over_selected = _changed(repo, base_sha, _BARE_PATHSPEC)

    assert set(over_selected) > set(_PACKAGE_ROOTS)
    assert any("/o/sha256/" in path for path in over_selected)
    assert _workflow_pathspec() != _BARE_PATHSPEC


def test_three_dot_diff_ignores_roots_the_base_advanced(tmp_path: Path) -> None:
    """Regression witness for the stale-announce-branch false positive
    (PRs #351/#423/#538), two halves.

    An announce branch is cut from main, then ANOTHER announce merges to
    main before this one validates. Three-dot (merge-base..HEAD) selects
    only the root this branch authored. Two-dot compares trees, so it also
    selects the OTHER package's root — whose stale head-side bytes a squash
    merge can never land — and hands it to `indexbot validate` to fail on.
    The second half proves the fixture discriminates; the first proves the
    workflow's spelling is immune.
    """
    repo = tmp_path / "index"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    other_root = repo / "p" / "other" / "pkg.json"
    other_root.parent.mkdir(parents=True)
    other_root.write_text('{"tag": "old"}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merge-base")

    _git(repo, "checkout", "-q", "-b", "announce")
    own_root = repo / "p" / "mine" / "pkg.json"
    own_root.parent.mkdir(parents=True)
    own_root.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "announce: curate mine/pkg")

    _git(repo, "checkout", "-q", "main")
    other_root.write_text('{"tag": "new"}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "announce: curate other/pkg")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "announce")

    three_dot = _changed(repo, base_sha, _workflow_pathspec())
    assert three_dot == ["p/mine/pkg.json"]

    two_dot = _git(
        repo,
        "diff",
        "--name-only",
        "--diff-filter=d",
        base_sha,
        "HEAD",
        "--",
        _workflow_pathspec(),
    ).split()
    assert sorted(two_dot) == ["p/mine/pkg.json", "p/other/pkg.json"]


def test_typechange_root_is_still_selected(tmp_path: Path) -> None:
    """A root swapped for a symlink is diff status `T` — the one status the
    old `ACMR` allowlist silently dropped, skipping `indexbot validate` while
    the required check went green. `--diff-filter=d` must keep it."""
    repo = tmp_path / "index"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    root = repo / "p" / "ns" / "pkg.json"
    root.parent.mkdir(parents=True)
    root.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    root.unlink()
    root.symlink_to("../../schema")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "swap root for symlink")

    assert _changed(repo, base_sha, _workflow_pathspec()) == ["p/ns/pkg.json"]
