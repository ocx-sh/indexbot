"""Read-only local `git` — `GitPort` implementation (ADR-4 BD-1's module map,
extended for the PR-validation lane).

**No new dependency.** `subprocess` is stdlib, so BD-1's "httpx is the only
runtime dependency" is unchanged. `git` itself is already a hard dependency
of every lane that can reach this code: the job that runs `indexbot
validate-pr` got its tree from a `git clone`, and the generated GitLab job
installs `git` in `before_script` precisely because it needs it here.

Every call is an argv list handed to `subprocess.run` with `shell=False`
(PY-PROC-04) — no interpolation ever reaches a shell, so a filename carrying
metacharacters is inert. `check=False` with the returncode read in the same
scope (PY-PROC-07), because git's own stderr is the only useful diagnostic
and `CalledProcessError` would discard it.

Output is captured as **bytes**, never `text=`: git paths are bytes on POSIX,
and a locale-dependent decode is the classic "fails on one CI image only"
bug (PY-PROC-01). `os.fsdecode` turns them back into `str` without ever
raising — a path that is not valid UTF-8 round-trips through surrogates and
is then rejected downstream by `core/validate_entry.py`'s package-id
grammar, which is the fail-closed outcome we want anyway.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404 - argv-only, shell=False; see module docstring
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ocx_indexbot.errors import ValidationError

_REF_RE: Final[re.Pattern[str]] = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]{0,254}")
"""Shape a ref/commit-ish must have before it is placed in an argv position.

Not injection defence — argv gives that for free. This stops **option**
injection: a ref beginning with `-` lands in `git diff`'s option position
(the `--` separator comes after the range), where an attacker-chosen ref
could select a different diff than the one the gate believes it is checking.
The length cap lives in the pattern's own `{0,254}` bound rather than in a
separate `len()` guard (ADR-4 BD-4 wants the cap, not a particular spelling
of it), and it is `fullmatch`, never `match`/`search`.
"""


@dataclass(frozen=True, slots=True)
class LocalGit:
    """`GitPort` over one checked-out repository."""

    repo: Path

    def changed_package_roots(self, base_sha: str, *, root_glob: str) -> tuple[str, ...]:
        """`git diff --name-only --diff-filter=d <base>...HEAD -- ':(glob)<root_glob>'`.

        Three of these four arguments are load-bearing, and each of them cost
        a production incident to learn:

        **`:(glob)`.** A git pathspec is not a shell glob — its `*` matches
        `/` too, so a bare `p/*/*.json` also selects every CAS object
        (`p/<ns>/<pkg>/o/sha256/<hex>.json`) a PR adds and hands it to
        `validate`, which rejects it as a malformed root. Since every
        announce adds a CAS object, that failed the required check on every
        announce PR. The magic prefix switches on `FNM_PATHNAME`, where `*`
        stops at a `/`. `core/policy.root_glob` builds the pattern itself,
        from the deployment's declared `name_segments`.

        **Three dots, never two.** Two-dot compares TREES, so an announce
        branch cut moments before another announce merged saw every root
        `main` had moved since its branch point as "changed", and re-verified
        the STALE HEAD COPY of packages the PR never touched against registry
        truth — digest mismatches on roots this PR never authored, whose
        stale bytes a squash merge can never land. Three-dot diffs against
        the merge base, selecting exactly the files the PR authored: the same
        file set the privileged governance gate classifies through the forge
        API, so both halves judge the same diff.

        **`--diff-filter=d`, an exclusion and not an allowlist.** A root
        swapped for a symlink is status `T`, which an `ACMR` allowlist
        silently dropped — zero roots selected, validation skipped, required
        check green. There is nothing to validate about a delete; every other
        status must reach `validate`.

        `-z` is the one addition to the shell step this replaces: it makes
        git emit raw NUL-separated paths instead of C-quoting anything
        non-ASCII, so parsing is exact rather than a second unquoting
        implementation. It changes none of the three properties above.
        """
        stdout = self._git(
            "diff",
            "--name-only",
            "--diff-filter=d",
            "-z",
            f"{_checked_ref(base_sha)}...HEAD",
            "--",
            f":(glob){root_glob}",
        )
        return tuple(os.fsdecode(entry) for entry in stdout.split(b"\0") if entry)

    def file_at(self, ref: str, path: str) -> bytes | None:
        """`git show <ref>:<path>`, or `None` when `path` does not exist at `ref`.

        Probed with `git cat-file -e` first rather than by pattern-matching
        `git show`'s failure text: absence is an ordinary answer here (a root
        this PR creates has no base-ref copy at all) and must never be
        confused with a repository that is broken, unfetched, or missing the
        ref entirely — those still raise.
        """
        target = f"{_checked_ref(ref)}:{path}"
        if self._run("cat-file", "-e", target).returncode != 0:
            return None
        return self._git("show", target)

    def _git(self, *args: str) -> bytes:
        """Run `git <args>` in `repo`, returning stdout; raise on any non-zero exit."""
        completed = self._run(*args)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ValidationError(
                f"git {' '.join(args)} failed (exit {completed.returncode}): {detail}"
            )
        return completed.stdout

    def _run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            # `git` is resolved through `$PATH` on purpose (S607/B607): the
            # runner owns `$PATH`, and hardcoding `/usr/bin/git` breaks every
            # image that ships it elsewhere — Alpine, nix, Homebrew.
            # Fixed argv, shell=False, `git` off `$PATH`.
            return subprocess.run(  # noqa: S603 # nosec B603 B607
                ["git", "-C", str(self.repo), *args],  # noqa: S607
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise ValidationError(f"cannot run git in {str(self.repo)!r}: {exc}") from exc


def _checked_ref(ref: str) -> str:
    """`ref` if it is shaped like a ref, else a `ValidationError` naming it."""
    if not _REF_RE.fullmatch(ref):
        raise ValidationError(f"{ref!r} is not a usable git ref")
    return ref
