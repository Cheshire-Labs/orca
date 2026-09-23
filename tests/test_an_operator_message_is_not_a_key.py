"""A not-found message must read as a sentence, not as a dict key.

Facades raise `KeyError` for "no such thing", which is the right Python
idiom. But `str()` on a KeyError is a repr, so the message an operator
reads arrives wrapped in an extra pair of quotes unless something
unwraps it. Every operation and daemon route that turns a KeyError into
a 404 goes through `message_of` for that reason.
"""

import pathlib
import re

from cheshire_source_text import code_lines_of, python_files

from orca.operations._protocol import message_of


class TestWhatTheOperatorReads:
    def test_a_key_errors_message_arrives_without_the_repr_quotes(self) -> None:
        exc = KeyError("Thread 't1' is not part of execution 'e1'")
        assert str(exc) == "\"Thread 't1' is not part of execution 'e1'\""
        assert message_of(exc) == "Thread 't1' is not part of execution 'e1'"

    def test_every_other_exception_reads_as_written(self) -> None:
        assert message_of(ValueError("cannot skip a finished method")) == (
            "cannot skip a finished method"
        )

    def test_a_key_error_subclass_is_unwrapped_too(self) -> None:
        class NoSuchSlot(KeyError):
            pass

        assert message_of(NoSuchSlot("slot 'a1' is not on this deck")) == (
            "slot 'a1' is not on this deck"
        )

    def test_a_key_error_raised_without_a_message_still_stringifies(self) -> None:
        assert message_of(KeyError()) == ""


class TestNothingSlipsBackIn:
    """A new `except <a KeyError> ... str(exc)` would put the quotes back."""

    def test_no_key_error_handler_stringifies_its_exception(self) -> None:
        src = pathlib.Path(__file__).resolve().parents[1] / "src"
        offenders = _handlers_that_stringify(src)
        assert offenders == [], (
            "these hand a KeyError's repr to an operator; use message_of(): "
            + ", ".join(offenders)
        )

    def test_the_scan_catches_a_bare_key_error(self, tmp_path) -> None:
        """Positive control: without it a broken scanner passes on a clean tree."""
        (tmp_path / "bad.py").write_text(
            _SOURCE.format(caught="KeyError"), encoding="utf-8",
        )
        assert _handlers_that_stringify(tmp_path) == ["bad.py:3"]

    def test_the_scan_catches_a_subclass_by_its_own_name(self, tmp_path) -> None:
        """The name in the `except` is usually not the word KeyError.

        `LabwareNotFound(KeyError)` is caught by its own name, and a scanner
        matching the text "KeyError" walks straight past it. Four live handlers
        were doing exactly that.
        """
        (tmp_path / "errors.py").write_text(
            _SUBCLASS, encoding="utf-8",
        )
        (tmp_path / "bad.py").write_text(
            _SOURCE.format(caught="NoSuchSlot"), encoding="utf-8",
        )
        assert _handlers_that_stringify(tmp_path) == ["bad.py:3"]

    def test_the_scan_catches_a_subclass_named_through_its_module(
        self, tmp_path,
    ) -> None:
        """`except errors.NoSuchSlot` is the same class as `except NoSuchSlot`."""
        (tmp_path / "errors.py").write_text(_SUBCLASS, encoding="utf-8")
        (tmp_path / "bad.py").write_text(
            _SOURCE.format(caught="errors.NoSuchSlot"), encoding="utf-8",
        )
        assert _handlers_that_stringify(tmp_path) == ["bad.py:3"]

    def test_the_scan_catches_a_tuple_holding_one(self, tmp_path) -> None:
        """A tuple that includes a KeyError can receive one, so it counts."""
        (tmp_path / "bad.py").write_text(
            _SOURCE.format(caught="(KeyError, ValueError)"), encoding="utf-8",
        )
        assert _handlers_that_stringify(tmp_path) == ["bad.py:3"]

    def test_the_scan_leaves_other_exceptions_alone(self, tmp_path) -> None:
        """Negative control: `str()` on a ValueError is how it should be read."""
        (tmp_path / "fine.py").write_text(
            _SOURCE.format(caught="ValueError"), encoding="utf-8",
        )
        assert _handlers_that_stringify(tmp_path) == []


_SOURCE = """\
try:
    f()
except {caught} as exc:
    raise E(str(exc))
"""

_SUBCLASS = """\
class NoSuchSlot(KeyError):
    pass
"""

_CLASS_DECL = re.compile(r"^class\s+(\w+)\s*\(([^)]*)\)")
_EXCEPT_DECL = re.compile(r"^except\s+\(?([\w\s,.]+?)\)?\s+as\s+(\w+)\s*:")


def _key_error_names(files, lines_of) -> set[str]:
    """Every name in this tree that resolves to a KeyError, transitively.

    Read off `class X(Base)` declarations rather than by import, because the
    scan must not drag the runtime in to answer a question about source text.
    """
    bases: dict[str, list[str]] = {}
    for path in files:
        for line in lines_of[path]:
            match = _CLASS_DECL.match(line.text.strip())
            if match:
                bases[match.group(1)] = [
                    b.strip().split(".")[-1] for b in match.group(2).split(",")
                ]
    names = {"KeyError"}
    grew = True
    while grew:
        grew = False
        for cls, parents in bases.items():
            if cls not in names and any(p in names for p in parents):
                names.add(cls)
                grew = True
    return names


def _handlers_that_stringify(root: pathlib.Path) -> list[str]:
    """`<path>:<line>` for every handler that hands a KeyError's repr onward."""
    files = sorted(python_files(root))
    lines_of = {path: code_lines_of(path) for path in files}
    key_errors = _key_error_names(files, lines_of)

    offenders: list[str] = []
    for path in files:
        lines = lines_of[path]
        for index, line in enumerate(lines):
            match = _EXCEPT_DECL.match(line.text.strip())
            if not match:
                continue
            # Last dotted segment: `except orca.errors.LabwareNotFound` names
            # the same class as a bare import of it.
            caught = {
                n.strip().split(".")[-1] for n in match.group(1).split(",")
            }
            if not caught & key_errors:
                continue
            var = match.group(2)
            for body in lines[index + 1:]:
                if body.indent <= line.indent:
                    break
                if f"str({var})" in body.text:
                    # Full path: a bare basename is ambiguous across the dozen
                    # __init__.py and routes.py in this tree.
                    offenders.append(
                        f"{path.relative_to(root).as_posix()}:{line.lineno}"
                    )
    return offenders
