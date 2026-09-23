"""Regression: shipped examples must `await` every call to a known async
method on the orca context / labware surface. A bare call returns a
coroutine, which is always truthy and silently dead-codes any conditional
that uses it (the original case was `if not ctx.labware.can_continue():`
at examples/smc_assay/workflow.py:282 -- a never-fired partial-fill exit).

Pyright does not flag `not <coroutine>` or `await missing` patterns
because they are syntactically valid, so this guard is the only catch.

Add a method name to KNOWN_ASYNC_METHODS_IN_EXAMPLES when a new async
method lands on a context type (ActionContext / MethodContext /
ThreadContext) or on LabwareInstance and could plausibly appear in an
example. False positives (sync methods of the same name on unrelated
receivers) are acceptable -- they're rare and force the example author
to clarify intent. The guard intentionally does not attempt type
inference; the explicit method list is the cost of staying static.
"""

import re
from pathlib import Path

import pytest

from cheshire_source_text import CodeLine, code_lines, code_lines_of


# A bare `obj.<name>()` call on one of these in an example is the
# class-of-bug this guard catches.
KNOWN_ASYNC_METHODS_IN_EXAMPLES: frozenset[str] = frozenset({
    # LabwareInstance (src/orca/resource_models/labware.py)
    "can_continue",
    "ops",
    # ActionContext / MethodContext / ThreadContext (src/orca/workflow_models/*)
    "wait_for",
    "emit",
    "manual_step",
})


def _call_pattern(names: frozenset[str]) -> re.Pattern[str]:
    """Match `<receiver>.<name>(` and capture whatever precedes the receiver,
    so the caller can tell an awaited call from a bare one."""
    alternatives = "|".join(sorted(re.escape(n) for n in names))
    return re.compile(rf"(\bawait\s+)?[\w.\]\)]+\.({alternatives})\s*\(")


def _find_unawaited_in_lines(
    lines: list[CodeLine], names: frozenset[str]
) -> list[tuple[str, int]]:
    """Return (method_name, line) for every call to a name in `names` that is
    not preceded by `await`."""
    pattern = _call_pattern(names)
    offenders: list[tuple[str, int]] = []
    for line in lines:
        for awaited, method in pattern.findall(line.text):
            if not awaited:
                offenders.append((method, line.lineno))
    return offenders


def _find_unawaited(source: str, names: frozenset[str]) -> list[tuple[str, int]]:
    return _find_unawaited_in_lines(code_lines(source), names)


def _find_unawaited_in_file(path: Path, names: frozenset[str]) -> list[tuple[str, int]]:
    return _find_unawaited_in_lines(code_lines_of(path), names)


def _iter_example_modules() -> list[Path]:
    examples_root = Path(__file__).resolve().parents[2] / "examples"
    return sorted(examples_root.rglob("*.py"))


@pytest.mark.parametrize(
    "snippet",
    [
        "if not ctx.labware.can_continue():\n    pass\n",
        "ctx.emit('x')\n",
        "result = ctx.wait_for(other)\n",
    ],
    ids=["negated-conditional", "bare-statement", "assigned"],
)
def test_scanner_flags_an_unawaited_call(snippet: str) -> None:
    """Positive control: the real guard passes vacuously once the examples are
    clean, so a silently broken scanner would never be caught by it."""
    assert _find_unawaited(snippet, KNOWN_ASYNC_METHODS_IN_EXAMPLES), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "if not await ctx.labware.can_continue():\n    pass\n",
        "await ctx.emit('x')\n",
        "result = await ctx.wait_for(other)\n",
        "# ctx.emit('x')\n",
        "helper.can_continue_later()\n",
    ],
    ids=["awaited-conditional", "awaited-statement", "awaited-assign", "comment", "longer-name"],
)
def test_scanner_ignores_awaited_calls(snippet: str) -> None:
    """Negative control: an over-eager scanner would fail every example."""
    assert not _find_unawaited(snippet, KNOWN_ASYNC_METHODS_IN_EXAMPLES), snippet


@pytest.mark.parametrize("module_path", _iter_example_modules(), ids=lambda p: p.name)
def test_example_awaits_known_async_methods(module_path: Path) -> None:
    offenders = _find_unawaited_in_file(module_path, KNOWN_ASYNC_METHODS_IN_EXAMPLES)
    assert not offenders, (
        f"{module_path}: {len(offenders)} unawaited async call(s): "
        f"{offenders}. These methods are async; calling without await "
        f"returns a truthy coroutine and dead-codes any conditional "
        f"that uses it. Extend KNOWN_ASYNC_METHODS_IN_EXAMPLES if a "
        f"false positive shows up on a same-named sync method."
    )
