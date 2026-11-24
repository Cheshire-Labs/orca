"""Guard: an `OperationError` is built from an error-code enum member, never from a bare string.

Read as text through `cheshire_source_text`, which blanks strings and comments and joins bracket continuations,
so a whole call sits on one line and a string literal shows as quotes around blanks.
"""

import re
from pathlib import Path

from cheshire_source_text import code_lines, code_lines_of, python_files

_SRC = Path(__file__).resolve().parent.parent / "src" / "orca"

_A_STRING = r"[rbfuRBFU]{0,2}[\"']"
_BARE_STRING_CODE = re.compile(
    rf"\bOperationError(?:\.of)?\(\s*(?:code\s*=\s*)?{_A_STRING}"
    rf"|\bOperationError(?:\.of)?\([^()]*\bcode\s*=\s*{_A_STRING}"
)


def _bare_string_codes(source_lines: list[str]) -> list[str]:
    return [line for line in source_lines if _BARE_STRING_CODE.search(line)]


def _lines(source: str) -> list[str]:
    return [line.text for line in code_lines(source)]


def test_no_operation_error_is_built_from_a_bare_string_code() -> None:
    found = [
        f"{path.relative_to(_SRC).as_posix()}:{line.lineno}: {line.text}"
        for path in python_files(_SRC)
        for line in code_lines_of(path)
        if _BARE_STRING_CODE.search(line.text)
    ]
    assert found == [], "pass an ErrorCode member, not a string:\n" + "\n".join(found)


def test_the_guard_fires_on_a_bare_string_code() -> None:
    assert len(_bare_string_codes(_lines('raise OperationError("not_found", "no labware")\n'))) == 1
    assert len(_bare_string_codes(_lines('raise OperationError.of(\n    "not_found",\n    "no labware",\n)\n'))) == 1
    assert len(_bare_string_codes(_lines('raise OperationError(message="x", code="not_found")\n'))) == 1


def test_the_guard_passes_an_enum_member_and_a_string_message() -> None:
    assert _bare_string_codes(_lines('raise OperationError.of(ErrorCode.LABWARE_NOT_FOUND, "no labware")\n')) == []
    assert _bare_string_codes(_lines('raise OperationError(code=ErrorCode.CONFLICT, message="busy")\n')) == []
    assert _bare_string_codes(_lines('raise OperationError.validation_error("bad shape")\n')) == []
