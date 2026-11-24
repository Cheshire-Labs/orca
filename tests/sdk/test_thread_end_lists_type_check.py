"""Pyright accepts every end list `orca.thread` documents, a `list[str]` variable included, and refuses the rest."""

import json
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

_CASES = Path(__file__).parent / "typing_cases"


class _Position(BaseModel):
    line: int


class _Range(BaseModel):
    start: _Position


class _Diagnostic(BaseModel):
    file: str
    severity: str
    message: str
    range: _Range


class _Report(BaseModel):
    generalDiagnostics: list[_Diagnostic]


def _error_lines(case_file: Path) -> dict[int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", "--pythonpath", sys.executable, str(case_file)],
        capture_output=True, text=True, timeout=300,
    )
    assert completed.stdout, f"pyright printed no report; install the dev extras (pip install -e .[dev]). stderr: {completed.stderr}"
    report = _Report.model_validate(json.loads(completed.stdout))
    return {
        diagnostic.range.start.line + 1: diagnostic.message
        for diagnostic in report.generalDiagnostics
        if diagnostic.severity == "error"
    }


def _lines_marked_refused(case_file: Path) -> set[int]:
    lines = case_file.read_text(encoding="utf-8").splitlines()
    return {number for number, line in enumerate(lines, start=1) if line.endswith("# refused")}


def test_pyright_accepts_every_documented_end_list() -> None:
    assert _error_lines(_CASES / "end_lists_accepted.py") == {}


def test_pyright_refuses_an_end_that_is_not_a_location_or_a_list_of_them() -> None:
    refused = _CASES / "end_lists_refused.py"
    marked = _lines_marked_refused(refused)
    assert len(marked) == 3
    assert set(_error_lines(refused)) == marked
