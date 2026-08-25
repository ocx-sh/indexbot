"""Tests for `adapters/local_git.py` — the PR-validation lane's diff.

Driven against a **real** repository built in `tmp_path`, never a mocked
`subprocess`. Every property this adapter exists to hold is a property of
git's own pathspec and revision-range semantics: a fake `subprocess.run`
would assert that we typed the arguments we meant to type, which is exactly
the thing that was already true of the shell steps these replace. Two of the
tests below (`..._skips_cas_objects`, `..._ignores_base_branch_movement`)
replay production incidents, and neither can fail against a mock.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ocx_indexbot.adapters.local_git import LocalGit
from ocx_indexbot.core.policy import root_glob
from ocx_indexbot.errors import ValidationError
from ocx_indexbot.ports import GitPort

_GLOB = root_glob(2)


def _git(repo: Path, *args: str) -> str:
    """Run one git command in `repo`, failing the test on any non-zero exit."""
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout


def _commit(repo: Path, message: str, files: dict[str, bytes]) -> str:
    """Write `files`, commit them, and return the new commit's sha."""
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one commit on `main`, and nothing else assumed —
    no user config from the developer's own machine, no signing, no hooks."""
    root = tmp_path / "index"
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.email", "bot@example.invalid")
    _git(root, "config", "user.name", "indexbot tests")
    _git(root, "config", "commit.gpgsign", "false")
    _commit(root, "seed", {"README.md": b"seed\n"})
    return root


# --- changed_package_roots ---------------------------------------------------------


def test_changed_package_roots_lists_added_and_modified_roots(repo: Path) -> None:
    """The ordinary case: a branch that adds one root and edits another
    reports both, and reports nothing else from the tree."""
    base = _commit(repo, "existing root", {"p/kitware/cmake.json": b'{"v":1}\n'})
    _git(repo, "checkout", "-b", "announce")
    _commit(
        repo,
        "announce",
        {"p/kitware/cmake.json": b'{"v":2}\n', "p/acme/tool.json": b"{}\n", "docs/x.md": b"x\n"},
    )

    git: GitPort = LocalGit(repo=repo)
    assert set(git.changed_package_roots(base, root_glob=_GLOB)) == {
        "p/kitware/cmake.json",
        "p/acme/tool.json",
    }


def test_changed_package_roots_skips_cas_objects(repo: Path) -> None:
    """Regression: `:(glob)` is load-bearing.

    A git pathspec's `*` matches `/` too, so a bare `p/*/*.json` also selects
    `p/<ns>/<pkg>/o/sha256/<hex>.json`. Every announce PR adds exactly such an
    object, so without the magic prefix `validate` was handed a CAS file as
    if it were a package root, rejected it as malformed, and failed the
    required check on every announce.
    """
    base = _commit(repo, "existing", {"p/kitware/cmake.json": b"{}\n"})
    _git(repo, "checkout", "-b", "announce")
    cas = "p/kitware/cmake/o/sha256/" + "a" * 64 + ".json"
    _commit(repo, "announce", {"p/kitware/cmake.json": b'{"v":2}\n', cas: b"{}\n"})

    changed = LocalGit(repo=repo).changed_package_roots(base, root_glob=_GLOB)
    assert changed == ("p/kitware/cmake.json",)


def test_changed_package_roots_ignores_base_branch_movement(repo: Path) -> None:
    """Regression: the range is three-dot, never two-dot.

    Two-dot compares TREES, so a branch cut before another announce merged
    saw every root `main` had moved since as "changed" and re-verified the
    STALE HEAD COPY of packages the PR never touched against registry truth.
    Here `main` moves `p/other/pkg.json` after the branch point; the branch
    never touches it, so it must not appear.
    """
    _commit(repo, "existing", {"p/other/pkg.json": b'{"v":1}\n'})
    _git(repo, "checkout", "-b", "announce")
    _commit(repo, "announce", {"p/kitware/cmake.json": b"{}\n"})
    _git(repo, "checkout", "main")
    _commit(repo, "unrelated merge on main", {"p/other/pkg.json": b'{"v":2}\n'})
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "announce")

    changed = LocalGit(repo=repo).changed_package_roots(base, root_glob=_GLOB)
    assert changed == ("p/kitware/cmake.json",)


def test_changed_package_roots_excludes_deletes_but_keeps_type_changes(repo: Path) -> None:
    """`--diff-filter=d` excludes deletes and nothing else.

    An allowlist (`ACMR`) silently dropped status `T` — a root swapped for a
    symlink — which meant zero roots selected, validation skipped, required
    check green. There is nothing to validate about a delete; every other
    status must reach `validate`.
    """
    base = _commit(repo, "existing", {"p/kitware/cmake.json": b"{}\n", "p/acme/gone.json": b"{}\n"})
    _git(repo, "checkout", "-b", "announce")
    (repo / "p" / "acme" / "gone.json").unlink()
    (repo / "p" / "kitware" / "cmake.json").unlink()
    (repo / "p" / "kitware" / "cmake.json").symlink_to("../acme/elsewhere.json")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "delete one root, symlink another")

    changed = LocalGit(repo=repo).changed_package_roots(base, root_glob=_GLOB)
    assert changed == ("p/kitware/cmake.json",)


def test_the_root_glob_cannot_see_the_package_tree_swapped_for_a_symlink(repo: Path) -> None:
    """Why `cli/validate_pr._check_package_tree_shape` exists, measured rather
    than argued.

    Deleting `p/` and committing a symlink under the same name repoints every
    published package. Git reports one added blob, `p`, plus a delete per file
    underneath — and `--diff-filter=d` drops the deletes, while `p/*/*.json`
    describes leaves and never the directory they hang from. So the root glob
    selects nothing at all, which is the required check going green over the
    widest change a pull request can make to this tree.
    """
    base = _commit(repo, "existing", {"p/kitware/cmake.json": b"{}\n", "elsewhere/x.json": b"{}\n"})
    _git(repo, "checkout", "-b", "swap")
    (repo / "p" / "kitware" / "cmake.json").unlink()
    (repo / "p" / "kitware").rmdir()
    (repo / "p").rmdir()
    (repo / "p").symlink_to("elsewhere")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "replace the package tree with a symlink")

    git = LocalGit(repo=repo)
    assert git.changed_package_roots(base, root_glob=_GLOB) == ()
    # `p/**` was the policy-less lane's pathspec, chosen as "the widest
    # possible" — and it is not: git reads a pathspec naming a directory as
    # matching its contents, so the two agree on everything *inside* the tree
    # and differ on exactly the path this branch changes.
    assert git.changed_package_roots(base, root_glob="p/**") == ()
    assert git.changed_package_roots(base, root_glob="p") == ("p",)


def test_the_widest_pathspec_is_a_superset_of_the_root_glob(repo: Path) -> None:
    """`p` selects the tree and everything in it, so the guard above costs the
    normal announce nothing: every path it returns for one is deeper than a
    root, and the guard only refuses what is shallower."""
    base = _commit(repo, "existing", {"p/kitware/cmake.json": b"{}\n"})
    _git(repo, "checkout", "-b", "announce")
    cas = "p/kitware/cmake/o/sha256/" + "a" * 64 + ".json"
    _commit(
        repo, "announce", {"p/kitware/cmake.json": b'{"x":1}\n', cas: b"{}\n", "docs/x.md": b"x\n"}
    )

    git = LocalGit(repo=repo)
    wide = git.changed_package_roots(base, root_glob="p")
    assert set(git.changed_package_roots(base, root_glob=_GLOB)) <= set(wide)
    assert wide == ("p/kitware/cmake.json", cas)
    assert all(path.count("/") >= 2 for path in wide), "nothing an announce touches is shallow"


def test_changed_package_roots_empty_when_the_branch_touches_no_root(repo: Path) -> None:
    """A docs-only PR selects nothing — the caller's "skip validation" case."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-b", "docs")
    _commit(repo, "docs", {"docs/x.md": b"x\n"})

    assert LocalGit(repo=repo).changed_package_roots(base, root_glob=_GLOB) == ()


def test_changed_package_roots_honours_the_declared_segment_count(repo: Path) -> None:
    """The glob comes from the deployment's `name_segments`, not a hardcoded
    two: a three-segment index sees its own roots and not a two-segment one."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-b", "announce")
    _commit(repo, "announce", {"p/eu/acme/tool.json": b"{}\n", "p/acme/tool.json": b"{}\n"})

    changed = LocalGit(repo=repo).changed_package_roots(base, root_glob=root_glob(3))
    assert changed == ("p/eu/acme/tool.json",)


def test_changed_package_roots_decodes_non_utf8_paths_without_raising(repo: Path) -> None:
    """A path that is not valid UTF-8 round-trips through surrogates rather
    than crashing the gate.

    `os.fsdecode` never raises, so the byte sequence survives to
    `core/validate_entry.py`'s package-id grammar, which rejects it — a
    validation failure the publisher can read, not a traceback. `-z` is what
    makes this reachable at all: without it git would C-quote the path and we
    would be unquoting it ourselves.
    """
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-b", "announce")
    weird = os.fsdecode(b"p/ns/caf\xe9.json")
    (repo / "p" / "ns").mkdir(parents=True)
    (repo / weird).write_bytes(b"{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "latin-1 filename")

    assert LocalGit(repo=repo).changed_package_roots(base, root_glob=_GLOB) == (weird,)


def test_changed_package_roots_raises_when_the_base_ref_is_unknown(repo: Path) -> None:
    """A base ref the checkout never fetched — the `fetch-depth: 0` mistake —
    fails loudly, naming git's own diagnostic, rather than reporting an empty
    diff and letting the gate pass with nothing validated."""
    with pytest.raises(ValidationError, match="git diff"):
        LocalGit(repo=repo).changed_package_roots("origin/never-fetched", root_glob=_GLOB)


def test_changed_package_roots_rejects_an_option_shaped_ref(repo: Path) -> None:
    """A ref beginning with `-` would land in `git diff`'s option position —
    the `--` separator comes after the range — and could select a different
    diff than the gate believes it is checking. Refused before argv is built."""
    with pytest.raises(ValidationError, match="not a usable git ref"):
        LocalGit(repo=repo).changed_package_roots("--output=/tmp/x", root_glob=_GLOB)


def test_changed_package_roots_rejects_an_overlong_ref(repo: Path) -> None:
    """The length cap is part of the ref pattern (ADR-4 BD-4: bound the input
    before matching it), so a megabyte of ref never reaches git at all."""
    with pytest.raises(ValidationError, match="not a usable git ref"):
        LocalGit(repo=repo).changed_package_roots("a" * 300, root_glob=_GLOB)


# --- file_at -----------------------------------------------------------------------


def test_file_at_returns_the_base_ref_bytes(repo: Path) -> None:
    """The bytes as they stand at the base ref, not as the PR rewrote them —
    ADR-2 ND-4 turns on telling a claim from an update."""
    base = _commit(repo, "existing", {"p/kitware/cmake.json": b'{"v":1}\n'})
    _git(repo, "checkout", "-b", "announce")
    _commit(repo, "announce", {"p/kitware/cmake.json": b'{"v":2}\n'})

    assert LocalGit(repo=repo).file_at(base, "p/kitware/cmake.json") == b'{"v":1}\n'


def test_file_at_returns_none_for_a_path_absent_at_that_ref(repo: Path) -> None:
    """A root the PR creates has no base-ref copy, and absence is an ordinary
    answer — `validate` then sees no base bytes and treats it as a new claim,
    which is the fail-closed reading ND-4 wants."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-b", "announce")
    _commit(repo, "new root", {"p/kitware/cmake.json": b"{}\n"})

    assert LocalGit(repo=repo).file_at(base, "p/kitware/cmake.json") is None


def test_file_at_rejects_an_option_shaped_ref(repo: Path) -> None:
    """Same option-injection guard as the diff, on the same grounds."""
    with pytest.raises(ValidationError, match="not a usable git ref"):
        LocalGit(repo=repo).file_at("-x", "p/kitware/cmake.json")


# --- process-level failures ---------------------------------------------------------


def test_a_missing_git_binary_is_a_validation_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git` absent from the image is a configuration failure the operator can
    act on (exit 1), never an `OSError` traceback out of the gate.

    An empty `$PATH` is how the generated GitLab job's own `before_script`
    guard (`command -v git`) can be wrong in practice: a slim image where the
    install silently failed.
    """
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ValidationError, match="cannot run git"):
        LocalGit(repo=repo).changed_package_roots("HEAD", root_glob=_GLOB)


def test_a_broken_repository_path_names_gits_own_diagnostic(tmp_path: Path) -> None:
    """git's stderr is carried into the error, because "which git command
    failed and why" is the only thing that tells a shallow clone apart from a
    genuinely absent ref."""
    with pytest.raises(ValidationError, match=r"git diff .* failed \(exit \d+\)"):
        LocalGit(repo=tmp_path / "does-not-exist").changed_package_roots("HEAD", root_glob=_GLOB)
