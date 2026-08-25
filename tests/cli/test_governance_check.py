from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ocx_indexbot.cli import governance_check
from ocx_indexbot.core.validate_entry import serialize_package_root
from ocx_indexbot.errors import ForgeError
from ocx_indexbot.model import Owner, PackageRoot, PullRequestInfo, TagEntry
from tests.fakes import FakeGitHub, make_policy

_OWNER = Owner(github="alice", github_id=1)
_OTHER_OWNER = Owner(github="bob", github_id=2)
_BASE = "base-sha"
_HEAD = "head-sha"
_ROOT_PATH = "p/kitware/cmake.json"
_STATUS_CONTEXT = "governance/review-required"
_MAINTAINERS_PATH = ".github/maintainers.yml"
_MAINTAINERS_YML = b"maintainers:\n  - github: carol\n    github_id: 99\n"
_COMMENT_MARKER = "<!-- indexbot:governance -->"


@pytest.fixture(autouse=True)
def _github_output(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """`run()` unconditionally writes the `disposition` `$GITHUB_OUTPUT` entry
    (see module docstring) — every test needs a target file for that write,
    not just the ones asserting on its contents."""
    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    return output_file


def _args(pr_number: int = 1) -> argparse.Namespace:
    return argparse.Namespace(pr_number=pr_number)


def _root(
    *,
    owners: tuple[Owner, ...] = (_OWNER,),
    tags: dict[str, TagEntry] | None = None,
) -> PackageRoot:
    return PackageRoot(
        name="ocx.sh/kitware/cmake",
        repository="oci://ghcr.io/kitware/cmake",
        owners=owners,
        status="active",
        deprecated_message=None,
        created="2026-07-17",
        desc=None,
        tags=dict(tags or {}),
    )


def _github(
    *,
    pr_number: int,
    base: PackageRoot | None,
    head: PackageRoot,
    author_login: str = "alice",
    author_id: int = 1,
    maintainers: bytes | None = _MAINTAINERS_YML,
    changed_paths: tuple[str, ...] = (_ROOT_PATH,),
    approvals: list[int] | None = None,
) -> FakeGitHub:
    files: dict[tuple[str, str], bytes] = {(_ROOT_PATH, _HEAD): serialize_package_root(head)}
    if base is not None:
        files[(_ROOT_PATH, _BASE)] = serialize_package_root(base)
    if maintainers is not None:
        files[(_MAINTAINERS_PATH, _BASE)] = maintainers
    info = PullRequestInfo(
        number=pr_number,
        base_sha=_BASE,
        head_sha=_HEAD,
        changed_paths=changed_paths,
        author_login=author_login,
        author_id=author_id,
    )
    return FakeGitHub(
        files=files,
        pull_request_info={pr_number: info},
        approvals={pr_number: approvals} if approvals is not None else {},
    )


def test_add_arguments_registers_pr_number() -> None:
    parser = argparse.ArgumentParser()
    governance_check.add_arguments(parser)
    args = parser.parse_args(["--pr-number", "7"])
    assert args.pr_number == 7


# --- G-19: refresh + author owns every touched package -> success ----------


def test_refresh_and_author_owns_package_sets_success_status(
    _github_output: Path,
) -> None:
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=before, head=after, author_login="alice", author_id=1)

    result = governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert result == governance_check.ExitCode.OK
    assert github.statuses[_HEAD] == [
        (
            _STATUS_CONTEXT,
            "success",
            "refresh: PR author owns every touched package, no review required",
        )
    ]
    # Green -> no reviewers assigned, no comment posted.
    assert github.requested_reviewers == {}
    assert github.comments == {}
    # G-19 machine-lane pass: validate.yml's auto-merge step reads this output.
    outputs = _github_output.read_text(encoding="utf-8")
    assert "disposition" in outputs
    assert "success" in outputs


def test_refresh_but_author_not_owner_falls_back_to_pending_with_reviewers(
    _github_output: Path,
) -> None:
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=before, head=after, author_login="mallory", author_id=999)

    result = governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert result == governance_check.ExitCode.OK
    context, state, description = github.statuses[_HEAD][0]
    assert context == _STATUS_CONTEXT
    assert state == "pending"
    assert "G-19" in description
    assert github.requested_reviewers[1] == ["carol"]
    assert _COMMENT_MARKER in github.comments[1][_COMMENT_MARKER]
    # Not machine-lane -> auto-merge must not be armed.
    assert "pending" in _github_output.read_text(encoding="utf-8")


def test_refresh_author_owns_one_of_two_touched_packages_falls_back_to_pending() -> None:
    before_a = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after_a = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    other_path = "p/acme/widget.json"
    before_b = _root(owners=(_OTHER_OWNER,))
    after_b = _root(owners=(_OTHER_OWNER,))
    github = _github(
        pr_number=1,
        base=before_a,
        head=after_a,
        author_login="alice",
        author_id=1,
        changed_paths=(_ROOT_PATH, other_path),
    )
    github.files[(other_path, _BASE)] = serialize_package_root(before_b)
    github.files[(other_path, _HEAD)] = serialize_package_root(after_b)

    result = governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert result == governance_check.ExitCode.OK
    _context, state, description = github.statuses[_HEAD][0]
    assert state == "pending"
    assert "G-19" in description


# --- human lane: new-package / human-review-required -----------------------


def test_new_package_classification_sets_pending_status_and_assigns_reviewers() -> None:
    github = _github(pr_number=1, base=None, head=_root())

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    context, state, description = github.statuses[_HEAD][0]
    assert context == _STATUS_CONTEXT
    assert state == "pending"
    assert "new-package" in description
    assert github.requested_reviewers[1] == ["carol"]
    assert 1 in github.comments


def test_human_review_required_classification_sets_pending_status() -> None:
    before = _root(owners=(_OWNER,))
    after = _root(owners=(_OTHER_OWNER,))
    github = _github(pr_number=1, base=before, head=after)

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    context, state, description = github.statuses[_HEAD][0]
    assert context == _STATUS_CONTEXT
    assert state == "pending"
    assert "human-review-required" in description


# --- G-20: reviewer self-review carve-out + missing maintainers.yml --------


def test_author_who_is_also_a_maintainer_is_excluded_from_reviewers() -> None:
    before = _root(owners=(_OWNER,))
    after = _root(owners=(_OTHER_OWNER,))
    maintainers = (
        b"maintainers:\n  - github: alice\n    github_id: 1\n  - github: carol\n    github_id: 99\n"
    )
    github = _github(
        pr_number=1, base=before, head=after, author_login="alice", maintainers=maintainers
    )

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.requested_reviewers[1] == ["carol"]


def test_no_reviewers_assigned_when_every_maintainer_is_the_author() -> None:
    before = _root(owners=(_OWNER,))
    after = _root(owners=(_OTHER_OWNER,))
    maintainers = b"maintainers:\n  - github: alice\n    github_id: 1\n"
    github = _github(
        pr_number=1, base=before, head=after, author_login="alice", maintainers=maintainers
    )

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.requested_reviewers == {}
    assert 1 in github.comments  # comment still posted even with no reviewers to assign


def test_missing_maintainers_file_assigns_no_reviewers_but_still_comments() -> None:
    before = _root(owners=(_OWNER,))
    after = _root(owners=(_OTHER_OWNER,))
    github = _github(pr_number=1, base=before, head=after, maintainers=None)

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.requested_reviewers == {}
    assert 1 in github.comments


def test_malformed_maintainers_file_assigns_no_reviewers_but_still_comments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A corrupt committed maintainers.yml must never crash the gate — same
    # graceful fallback as a missing file entirely, plus a logged stderr
    # line (never a silently swallowed error).
    before = _root(owners=(_OWNER,))
    after = _root(owners=(_OTHER_OWNER,))
    github = _github(pr_number=1, base=before, head=after, maintainers=b"not: valid\nmaintainers\n")

    result = governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert result == governance_check.ExitCode.OK
    assert github.requested_reviewers == {}
    assert 1 in github.comments
    assert "malformed maintainers.yml ignored" in capsys.readouterr().err


def test_comment_is_idempotent_across_repeated_runs() -> None:
    before = _root(owners=(_OWNER,))
    after = _root(owners=(_OTHER_OWNER,))
    github = _github(pr_number=1, base=before, head=after)

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())
    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    # One marker key, updated in place — never a second, distinct comment.
    assert list(github.comments[1]) == [_COMMENT_MARKER]


def test_run_missing_pull_request_propagates_key_error() -> None:
    github = FakeGitHub()
    with pytest.raises(KeyError):
        governance_check.run(_args(pr_number=99), github=github, policy=make_policy())


# --- _author_owns_every_touched_package (direct unit test) ------------------


def test_author_owns_every_touched_package_true_when_id_matches() -> None:
    root = _root(owners=(_OWNER,))
    github = _github(pr_number=1, base=root, head=root, author_id=1)
    info = github.get_pull_request_info(1)
    owns = governance_check._author_owns_every_touched_package(  # pyright: ignore[reportPrivateUsage]
        info, github, policy=make_policy()
    )
    assert owns is True


def test_author_owns_every_touched_package_false_when_base_root_missing() -> None:
    # Not reachable via `run()` for a "refresh"-classified PR in practice
    # (classify_pull_request would call this "new-package" instead) — this
    # helper's own contract is still exercised directly.
    root = _root(owners=(_OWNER,))
    info = PullRequestInfo(
        number=1,
        base_sha=_BASE,
        head_sha=_HEAD,
        changed_paths=(_ROOT_PATH,),
        author_login="alice",
        author_id=1,
    )
    github = FakeGitHub(files={(_ROOT_PATH, _HEAD): serialize_package_root(root)})
    owns = governance_check._author_owns_every_touched_package(  # pyright: ignore[reportPrivateUsage]
        info, github, policy=make_policy()
    )
    assert owns is False


# --- the human lane's exit: approvals bind on github_id --------------------


def test_a_maintainers_approval_by_github_id_releases_the_human_lane(
    _github_output: Path,
) -> None:
    """The human lane's only exit. `ForgePort.list_approvals` reports numeric
    user ids, and the committed maintainer is `carol`, `github_id: 99` — so
    `99` approving is `carol` approving, and the gate goes green naming her.

    This is the half a revert to login matching breaks: with `_approver`
    comparing ids against `maintainers.yml`'s `github` logins, nothing matches
    and this PR stays `pending` forever.
    """
    head = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=None, head=head, approvals=[99])

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.statuses[_HEAD] == [
        (_STATUS_CONTEXT, "success", "new-package: approved by carol")
    ]


def test_a_recycled_maintainer_login_does_not_release_the_human_lane(
    _github_output: Path,
) -> None:
    """The reason the id is the key. A GitHub login is renameable and, once
    released, registrable by anyone; the account below holds the *name* the
    committed maintainer used to have and a different id. An approval
    outranks every disposition, `governance.auto_merge = never` included, so
    admitting one on a name would hand the human lane's exit to a stranger
    with no change to any committed file.
    """
    head = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=None, head=head, approvals=[4242])

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.statuses[_HEAD] == [
        (_STATUS_CONTEXT, "pending", "new-package: awaiting human review")
    ]


def test_the_self_review_carve_out_binds_on_id_not_on_the_authors_current_login(
    _github_output: Path,
) -> None:
    """`maintainers.yml` records the author under an old login (`alice-old`,
    id 1) while the PR reports their current one (`alice`). The carve-out has
    to recognise them as the same person, or the author is assigned as their
    own reviewer — which GitHub's API rejects outright — and their own
    approval releases the lane they are meant to be waiting in.
    """
    maintainers = (
        b"maintainers:\n  - github: alice-old\n    github_id: 1\n"
        b"  - github: carol\n    github_id: 99\n"
    )
    head = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=None, head=head, maintainers=maintainers, approvals=[1])

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.requested_reviewers[1] == ["carol"]
    assert github.statuses[_HEAD] == [
        (_STATUS_CONTEXT, "pending", "new-package: awaiting human review")
    ]


# --- governance.auto_merge: the dial over `_disposition` --------------------


def test_auto_merge_never_sends_even_an_owned_refresh_to_the_human_lane(
    _github_output: Path,
) -> None:
    """`never` is for an index that wants a person on every change. The PR
    below is the strongest possible machine-lane case — a tag refresh by the
    package's own owner — and it still gets reviewers and a comment."""
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=before, head=after, author_login="alice", author_id=1)

    result = governance_check.run(
        _args(pr_number=1), github=github, policy=make_policy(auto_merge="never")
    )

    assert result == governance_check.ExitCode.OK
    assert github.statuses[_HEAD] == [
        (_STATUS_CONTEXT, "pending", "refresh: policy sets governance.auto_merge = never")
    ]
    assert github.requested_reviewers == {1: ["carol"]}
    assert "success" not in _github_output.read_text(encoding="utf-8")


def test_auto_merge_always_greens_a_refresh_by_a_non_owner(_github_output: Path) -> None:
    """`always` drops G-19, which only makes sense where the forge already
    decides who may open a PR at all. The author here owns nothing."""
    before = _root(
        owners=(_OTHER_OWNER,),
        tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")},
    )
    after = _root(
        owners=(_OTHER_OWNER,),
        tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")},
    )
    github = _github(pr_number=1, base=before, head=after, author_login="mallory", author_id=404)

    result = governance_check.run(
        _args(pr_number=1), github=github, policy=make_policy(auto_merge="always")
    )

    assert result == governance_check.ExitCode.OK
    assert github.statuses[_HEAD] == [
        (_STATUS_CONTEXT, "success", "refresh: policy sets governance.auto_merge = always")
    ]
    assert github.requested_reviewers == {}


def test_auto_merge_always_still_sends_a_new_package_to_a_human(_github_output: Path) -> None:
    """The dial moves the ownership line, never the classification line: a
    `new-package` claim needs a human under every setting, because no
    ownership check would have caught it either — there is nothing to own
    yet."""
    after = _root()
    github = _github(pr_number=1, base=None, head=after, author_login="alice", author_id=1)

    result = governance_check.run(
        _args(pr_number=1), github=github, policy=make_policy(auto_merge="always")
    )

    assert result == governance_check.ExitCode.OK
    assert github.statuses[_HEAD] == [
        (_STATUS_CONTEXT, "pending", "new-package: awaiting human review")
    ]
    assert github.requested_reviewers == {1: ["carol"]}


def test_auto_merge_always_never_reads_the_base_ref_owners() -> None:
    """Not an optimisation — a statement about what the decision depends on.
    Under `owners` the base-ref root is read twice: once to classify, once
    for G-19. Under `always` ownership is not part of the answer, so the
    second read must not happen at all."""
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})

    def _count_base_root_reads(auto_merge: str) -> int:
        github = _github(pr_number=1, base=before, head=after)
        reads: list[tuple[str, str]] = []
        original = github.get_file_contents

        def _spy(path: str, ref: str) -> bytes | None:
            reads.append((path, ref))
            return original(path, ref)

        github.get_file_contents = _spy  # pyright: ignore[reportAttributeAccessIssue]
        governance_check.run(
            _args(pr_number=1), github=github, policy=make_policy(auto_merge=auto_merge)
        )
        return reads.count((_ROOT_PATH, _BASE))

    assert _count_base_root_reads("owners") == 2
    assert _count_base_root_reads("always") == 1


# --- ordering: the block goes up first and comes down last -----------------


def test_the_blocking_thread_is_opened_before_the_status_it_reports(
    _github_output: Path,
) -> None:
    """A status write is one API call that can refuse for reasons unrelated
    to the decision — a GitLab commit status is a state machine, and this gate
    re-runs against the same head on every poll tick. Made first, a refusal
    ends the run before the review-required thread is re-opened, and on a fork
    merge request that thread is the only thing holding the merge."""
    head = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=None, head=head)

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.calls.index("create_comment") < github.calls.index("set_commit_status")


def test_a_status_write_that_fails_leaves_the_merge_request_blocked(
    _github_output: Path,
) -> None:
    """The direction to fail in. A blocked merge request with a stale status
    beside it costs a human one look; an unblocked one costs the gate."""
    head = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=None, head=head)
    github.status_error = ForgeError("GitLab API refused to POST /statuses (400)")

    with pytest.raises(ForgeError):
        governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.unresolved[1], "the review-required thread is still blocking"


def test_the_thread_is_released_only_after_the_green_status_is_recorded(
    _github_output: Path,
) -> None:
    """The mirror of the rule above: releasing the gate before the status it
    reports is recorded would let a failed status write leave an unblocked
    merge request with nothing green to justify it."""
    before = _root(tags={"1.0.0": TagEntry(content="sha256:" + "a" * 64, observed="T0")})
    after = _root(tags={"1.0.0": TagEntry(content="sha256:" + "b" * 64, observed="T1")})
    github = _github(pr_number=1, base=before, head=after)

    governance_check.run(_args(pr_number=1), github=github, policy=make_policy())

    assert github.calls.index("set_commit_status") < github.calls.index("resolve_review_thread")
