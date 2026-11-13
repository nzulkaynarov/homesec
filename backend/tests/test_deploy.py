"""The Pi installs a commit only when GitHub says every check passed."""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ci_verdict", Path(__file__).resolve().parents[2] / "deploy" / "ci_verdict.py"
)
assert _SPEC is not None and _SPEC.loader is not None
ci_verdict = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci_verdict)


def _runs(*runs: tuple[str, str | None]) -> str:
    # GitHub pretty-prints its JSON with a space after every colon;
    # the old gate missed exactly that.
    checks = [{"status": s, "conclusion": c} for s, c in runs]
    body = {"total_count": len(runs), "check_runs": checks}
    return json.dumps(body, indent=2)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_runs(("completed", "success")), "green"),
        (_runs(("completed", "success"), ("completed", "skipped")), "green"),
        (_runs(("completed", "failure")), "red"),
        (_runs(("completed", "success"), ("completed", "cancelled")), "red"),
        (_runs(("in_progress", None)), "wait"),
        (_runs(("queued", None), ("completed", "success")), "wait"),
        (_runs(), "wait"),  # no checks yet: GitHub has not started CI
        ('{"message": "API rate limit exceeded"}', "wait"),
        ("", "wait"),
        ("<html>", "wait"),
    ],
)
def test_verdict(raw: str, expected: str) -> None:
    assert ci_verdict.verdict(raw) == expected


def test_live_shape_is_understood() -> None:
    assert '": "' in _runs(("completed", "success"))  # the spacing that broke the old string match
