"""The shipped JSON Schema and `parse_index_policy` must agree.

`core/policy.py` is the runtime authority — ADR-4 BD-1 keeps `httpx` the only
runtime dependency, so the bot never loads a schema validator. The schema in
`ocx_indexbot/schema/` exists so an operator's editor can autocomplete
`.github/index-policy.json` and pre-flight it in CI. Two grammars describing
one file is exactly the setup that drifts, and drift here is worse than
useless: an editor that blesses a config the bot will reject teaches the
operator to distrust the editor.

So neither validates against the other — they are both run against one
corpus. Every file under `fixtures/policy/accept/` must be accepted by both,
every file under `fixtures/policy/reject/` rejected by both. A new grammar
rule arrives as a fixture pair, and a rule added to only one side fails here.

**One deliberate divergence, kept out of the corpus.** JSON Schema's
`"type": "integer"` accepts `2.0` (a number with no fractional part);
`parse_index_policy` requires a Python `int` and rejects it. Both refuse
`true`, `"2"` and `null`, which are the shapes a hand-edited file actually
produces. `2.0` is covered by `test_policy.py`'s own unit tests rather than
papered over by loosening the parser.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ocx_indexbot.cli.schema_cmd import read_schema
from ocx_indexbot.core.policy import parse_index_policy
from ocx_indexbot.errors import ValidationError

_CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "policy"


def _cases(kind: str) -> list[Path]:
    files = sorted((_CORPUS / kind).glob("*.json"))
    assert files, f"corpus directory {kind}/ is empty — the agreement test would pass vacuously"
    return files


def _validator() -> Draft202012Validator:
    """Built from the *shipped* schema text, read the same way the CLI reads
    it — never a copy pasted into the test, which could pass while the wheel
    carried something else."""
    return Draft202012Validator(json.loads(read_schema()))


def _schema_errors(raw: bytes) -> list[str]:
    """Schema violations as plain messages.

    The `pyright: ignore` is a stub gap, not a shortcut: `iter_errors` is
    declared with an overload whose element type is `Any`, so strict mode
    reports the member itself as partially unknown no matter how the result is
    annotated here.
    """
    document = json.loads(raw)
    errors = _validator().iter_errors(document)  # pyright: ignore[reportUnknownMemberType]
    return sorted(str(error.message) for error in errors)


def test_the_schema_is_itself_valid() -> None:
    Draft202012Validator.check_schema(json.loads(read_schema()))


@pytest.mark.parametrize("path", _cases("accept"), ids=lambda p: p.stem)
def test_accepted_by_both(path: Path) -> None:
    raw = path.read_bytes()
    parse_index_policy(raw)  # must not raise
    errors = _schema_errors(raw)
    assert not errors, f"schema rejected an accepted config: {errors}"


@pytest.mark.parametrize("path", _cases("reject"), ids=lambda p: p.stem)
def test_rejected_by_both(path: Path) -> None:
    raw = path.read_bytes()
    with pytest.raises(ValidationError):
        parse_index_policy(raw)
    assert _schema_errors(raw), (
        "parser rejected this config but the schema accepted it — the editor would lie"
    )
