"""Guard: a test's timeout mark must exceed every ceiling inside the test.

Sibling of ``test_no_wallclock_sync_guard``, which bans wall-clock synchronization
in a test *body*. This one catches the same flake class arriving through a
*decorator*.

A test that is killed at N seconds cannot reach an inner ``wait_for(..., M)``
when M > N. That is not a style opinion, it is a contradiction: the inner
ceiling is dead code, and the mark -- not the assertion -- decides the verdict.
Every such pair is a number nobody reconciled, and it always resolves the same
way: the mark was copied in bulk, the ceiling was written on purpose.

Why it flakes rather than fails outright: the sim drivers wait a flat
``SleepSim.sim_time`` (0.2s) per driver call and reservation contention retries
on ``ReservationConfig.retry_interval`` (0.5s), so a runtime test costs seconds
by construction and that cost scales with runner load. A mark set below the
test's own declared ceiling leaves a margin measured against nothing, and a
loaded runner spends it. The test then fails for the machine it ran on rather
than the behavior it asserts.

Fixing a violation means making the two numbers agree:

* Fast suite: delete the mark. ``pyproject``'s ``addopts`` carries the global
  ``--timeout``, which bounds the hang, and the inner ``wait_for`` names the
  step that stalled instead of killing the process at an arbitrary second.
* Slow suite: raise the mark above the inner ceiling. Those runs clear
  ``addopts`` (``--override-ini``), so there is no global bound behind them and
  the mark is the only hang-safety.

It bans the CONTRADICTION, never a tight mark on its own. A deliberate small
mark whose margin the test provably cannot approach is not a defect. Both current
examples await something that never completes, so nothing else in the test bounds
the wait and the mark is the only fast-failure path:
``tests/runtime/test_await_result_or_deadline.py`` and
``test_timeout_fails_loudly_with_thread_dump`` in
``tests/test_execution_outcome_helper.py``. A ban on tight marks would delete both,
which is why the rule compares two declared numbers instead.

This guard has no opt-out by design: a flagged pair is reconciled, not suppressed.

It matches wait helpers by name, which makes it sound but not exhaustive: a wait
missing from ``_CEILING_CALLS`` can hide a contradiction, but no listed one can
invent a false positive. That asymmetry is the right direction for a guard, and
it is why the rule is a provable contradiction rather than a judgement about
which tests "look slow" (such a heuristic was tried and missed most of them,
because tests reach the runtime through fixtures rather than by naming a helper).
"""

import re
from pathlib import Path

import pytest

from cheshire_source_text import CodeLine, code_lines, code_lines_of

_TESTS_DIR = Path(__file__).parent
_SELF = Path(__file__).name

_ADDOPTS_TIMEOUT = re.compile(r"^addopts\s*=.*--timeout=(\d+(?:\.\d+)?)", re.MULTILINE)


def _global_fast_timeout() -> float | None:
    """The fast suite's hang-safety, and the cap on any test carrying no mark.

    Read from ``pyproject`` rather than restated here, so this guard cannot
    become the next unreconciled number it exists to catch.
    """
    found = _ADDOPTS_TIMEOUT.search(
        (_TESTS_DIR.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    return float(found.group(1)) if found else None


_GLOBAL_FAST_TIMEOUT = _global_fast_timeout()

# Calls whose timeout argument is a ceiling the test intends to reach. Add a name
# here when a new wait helper grows a timeout argument.
_CEILING_CALLS = {
    "wait_for",
    "wait",
    "wait_for_execution",
    "execution_outcome",
    "wait_until",
    "run_to_quiescence",
    "wait_for_runtime_condition",
    "wait_for_paused_thread",
    "wait_for_paused_threads",
}

_NUMBER = r"\d+(?:\.\d+)?"
_TIMEOUT_MARK = re.compile(rf"\.timeout\s*\(\s*(?:timeout\s*=\s*)?({_NUMBER})\s*[,)]")
_SLOW_MARK = re.compile(r"\.slow\b")
_CLASS = re.compile(r"^class\s+(\w+)")
_TEST_DEF = re.compile(r"^(?:async\s+)?def\s+(test\w*)")
_PYTESTMARK = re.compile(r"^pytestmark\s*=")
_TIMEOUT_ARG = re.compile(rf"^timeout\s*=\s*({_NUMBER})$")
_BARE_NUMBER = re.compile(rf"^{_NUMBER}$")


def _scanned_files() -> list[Path]:
    return sorted(p for p in _TESTS_DIR.rglob("test_*.py") if p.name != _SELF)


def _mark_value(decorators: str) -> float | None:
    """The seconds a ``@pytest.mark.timeout(N)`` carries, positional or by keyword."""
    found = _TIMEOUT_MARK.search(decorators)
    return float(found.group(1)) if found else None


def _has_slow_mark(decorators: str) -> bool:
    return _SLOW_MARK.search(decorators) is not None


def _call_arguments(text: str, open_paren: int) -> str:
    """The text between `open_paren` and its matching close paren."""
    depth = 0
    for index in range(open_paren, len(text)):
        if text[index] in "([{":
            depth += 1
        elif text[index] in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:index]
    return text[open_paren + 1:]


def _split_top_level(arguments: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in arguments:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def _inner_ceilings(line: CodeLine) -> list[float]:
    """Literal timeout ceilings the line asks for."""
    found: list[float] = []
    for call in _CEILING_CALLS:
        for match in re.finditer(rf"(?<!\w){re.escape(call)}\s*\(", line.text):
            arguments = _call_arguments(line.text, line.text.index("(", match.start()))
            # Only this call's own arguments count: a nested wait carries its
            # own ceiling and is scanned when its own name matches.
            for position, argument in enumerate(_split_top_level(arguments)):
                keyword = _TIMEOUT_ARG.match(argument)
                if keyword:
                    found.append(float(keyword.group(1)))
                elif position and _BARE_NUMBER.match(argument):
                    found.append(float(argument))
    return found


def _violations_in_lines(lines: list[CodeLine], label: str) -> list[str]:
    found: list[str] = []
    module_marks = "".join(line.text for line in lines if _PYTESTMARK.match(line.text))
    module_cap = _mark_value(module_marks)
    module_slow = _has_slow_mark(module_marks)

    classes: list[tuple[int, float | None, bool]] = []
    decorators = ""
    index = 0

    while index < len(lines):
        line = lines[index]
        index += 1
        while classes and line.indent <= classes[-1][0]:
            classes.pop()

        if line.text.startswith("@"):
            decorators += " " + line.text
            continue

        inherited_cap = classes[-1][1] if classes else module_cap
        inherited_slow = classes[-1][2] if classes else module_slow

        if _CLASS.match(line.text):
            own = _mark_value(decorators)
            classes.append((
                line.indent,
                inherited_cap if own is None else own,
                inherited_slow or _has_slow_mark(decorators),
            ))
            decorators = ""
            continue

        test = _TEST_DEF.match(line.text)
        if test is None:
            decorators = ""
            continue

        own = _mark_value(decorators)
        is_slow = inherited_slow or _has_slow_mark(decorators)
        decorators = ""
        effective = own if own is not None else inherited_cap
        if effective is None and not is_slow:
            effective = _GLOBAL_FAST_TIMEOUT

        body_start = index
        while index < len(lines) and lines[index].indent > line.indent:
            index += 1
        if effective is None:
            continue

        for body_line in lines[body_start:index]:
            breached = [c for c in _inner_ceilings(body_line) if c >= effective]
            if not breached:
                continue
            found.append(
                f"{label}:{body_line.lineno}: {test.group(1)} waits up to {breached[0]:g}s but its "
                f"timeout mark kills it at {effective:g}s. The mark's clock starts "
                f"at setup, so it must EXCEED every inner ceiling or the wait's "
                f"own failure dump never prints. Drop the mark (fast suite keeps "
                f"the global --timeout) or raise it above {breached[0]:g}s."
            )
            break

    return found


def _violations_in_source(source: str) -> list[str]:
    return _violations_in_lines(code_lines(source), "<snippet>")


def _violations(path: Path) -> list[str]:
    return _violations_in_lines(code_lines_of(path), str(path))


_CONTRADICTION = (
    "import pytest\n"
    "@pytest.mark.timeout(5)\n"
    "async def test_x():\n"
    "    await wait_for(thing, timeout=30)\n"
)
_CLASS_CONTRADICTION = (
    "import pytest\n"
    "@pytest.mark.timeout(5)\n"
    "class TestGroup:\n"
    "    async def test_x(self):\n"
    "        await wait_for(thing, timeout=30)\n"
)
_MODULE_CONTRADICTION = (
    "import pytest\n"
    "pytestmark = pytest.mark.timeout(5)\n"
    "async def test_x():\n"
    "    await wait_for(thing, timeout=30)\n"
)
_UNMARKED_OVER_GLOBAL = (
    "async def test_x():\n"
    "    await wait_for(thing, timeout=300)\n"
)
_POSITIONAL_CEILING = (
    "import pytest\n"
    "@pytest.mark.timeout(5)\n"
    "async def test_x():\n"
    "    await asyncio.wait_for(thing, 30)\n"
)


@pytest.mark.parametrize(
    "snippet",
    [_CONTRADICTION, _CLASS_CONTRADICTION, _MODULE_CONTRADICTION,
     _UNMARKED_OVER_GLOBAL, _POSITIONAL_CEILING],
    ids=["own-mark", "class-mark", "module-mark", "global-fast-cap", "positional"],
)
def test_scanner_flags_each_contradiction(snippet: str) -> None:
    """Positive control: the real guard passes vacuously once the suite is
    reconciled, so a silently broken scanner would never be caught by it."""
    assert _violations_in_source(snippet), f"scanner failed to flag:\n{snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "import pytest\n@pytest.mark.timeout(60)\nasync def test_x():\n    await wait_for(t, timeout=30)\n",
        "import pytest\n@pytest.mark.slow\nasync def test_x():\n    await wait_for(t, timeout=300)\n",
        "async def test_x():\n    await wait_for(t, timeout=30)\n",
        "import pytest\n@pytest.mark.timeout(5)\nasync def test_x():\n    await other_helper(t, timeout=30)\n",
        "import pytest\n@pytest.mark.timeout(5)\nasync def test_x():\n    pass\nasync def test_y():\n    await wait_for(t, timeout=30)\n",
        "import pytest\npytestmark = pytest.mark.timeout(5)\nasync def test_x():\n    await wait_for(t, timeout=0.02)\n",
    ],
    ids=["mark-exceeds", "slow-unbounded", "under-global-cap", "unlisted-helper",
         "mark-not-inherited-by-sibling", "deliberate-tight-mark"],
)
def test_scanner_ignores_reconciled_pairs(snippet: str) -> None:
    """Negative control: an over-eager scanner would fail the whole suite."""
    assert not _violations_in_source(snippet), f"scanner false-positived on:\n{snippet}"


def test_the_fast_suite_still_declares_a_global_timeout() -> None:
    """The cap this guard applies to an unmarked test. If ``addopts`` ever drops
    ``--timeout``, those tests silently stop being scanned instead of failing."""
    assert _GLOBAL_FAST_TIMEOUT is not None, (
        "pyproject's [tool.pytest.ini_options] addopts no longer carries "
        "--timeout=N, so a test with no mark of its own has no cap to compare "
        "an inner ceiling against and this guard skips it. Restore it."
    )


def test_timeout_marks_do_not_contradict_inner_ceilings() -> None:
    violations: list[str] = []
    for path in _scanned_files():
        violations.extend(_violations(path))
    assert not violations, (
        "Timeout marks that contradict the test's own ceiling (fix, do not suppress):\n"
        + "\n".join(violations)
    )
