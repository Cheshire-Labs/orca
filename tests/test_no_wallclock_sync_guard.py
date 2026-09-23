"""Guard: tests must not synchronize on the wall clock.

A test may only pass-always (waits on a condition/event with a generous
ceiling) or fail-always (a reliable reproducer). "Sometimes fails" is banned,
and its two usual sources are banned here structurally:

1. Asserting on measured elapsed time -- a ``time.monotonic()`` /
   ``perf_counter()`` / ``time.time()`` delta compared in an assert, written
   either inline (``assert monotonic() - t0 < N``) or via a variable
   (``elapsed = monotonic() - t0; assert elapsed < N``). Flakes on a loaded
   runner.
2. ``asyncio.sleep()`` / ``time.sleep()`` with a short positive literal used as
   a synchronization guess (not a poll interval inside a loop). Flakes when the
   awaited work takes longer than the guess.

Only a *delta fed into an assert* is banned: a bare timestamp
(``t0 = time.time()``) and a poll ceiling (``deadline = monotonic() + timeout``,
an addition, typically in a ``while`` loop) are fine. Wait on a condition
instead. This guard has no opt-out by design: a flagged line is fixed, not
suppressed.
"""

import re
from pathlib import Path

import pytest

from cheshire_source_text import CodeLine, code_lines, code_lines_of

_TESTS_DIR = Path(__file__).parent
_SELF = Path(__file__).name

# sleep(>= this) is a block-until-cancelled stub (e.g. sleep(3600)), not a
# synchronization guess. sleep(0) is a cooperative yield. Both are allowed.
_SLEEP_SENTINEL_MIN = 30.0

# A subtraction touching one of these is a wall-clock elapsed measurement. The
# call may sit on either side of the minus.
_ELAPSED_CALL = r"(?:[\w.]*\b(?:perf_counter|monotonic|time)\s*\(\s*\))"
_DELTA = re.compile(rf"{_ELAPSED_CALL}\s*-|-\s*{_ELAPSED_CALL}")

_ASSERT = re.compile(r"^assert\b(.*)$")
_ASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=]+?)?=(?!=)\s*(.+)$")
_NAME = re.compile(r"[A-Za-z_]\w*")
_SLEEP = re.compile(r"\bsleep\s*\(\s*(\d+(?:\.\d+)?)\s*[,)]")
_LOOP_HEADER = re.compile(r"^(?:async\s+)?(?:for|while)\b.*:")
_BLOCK_HEADER = re.compile(r"^(?:async\s+)?(?:for|while|if|elif|else|try|except|finally|with|def|class)\b.*:")


def _scanned_files() -> list[Path]:
    files = [p for p in _TESTS_DIR.rglob("test_*.py") if p.name != _SELF]
    files += list(_TESTS_DIR.rglob("conftest.py"))
    return sorted(files)


def _contains_elapsed_delta(text: str) -> bool:
    """True if `text` computes a wall-clock delta: a subtraction with a
    monotonic()/perf_counter()/time() call on either side."""
    return _DELTA.search(text) is not None


def _elapsed_tainted_names(lines: list[CodeLine]) -> set[str]:
    """Names bound to a wall-clock delta, directly (``e = monotonic() - t0``) or
    through one another (``e2 = e1``). Iterated to a fixed point so aliasing of
    an already-tainted name is also caught."""
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for line in lines:
            assigned = _ASSIGN.match(line.text)
            if not assigned:
                continue
            name, value = assigned.group(1), assigned.group(2)
            if name in tainted:
                continue
            if _contains_elapsed_delta(value) or value.strip() in tainted:
                tainted.add(name)
                changed = True
    return tainted


def _lines_inside_loops(lines: list[CodeLine]) -> set[int]:
    """Line numbers sitting inside a ``for`` / ``while`` body."""
    inside: set[int] = set()
    open_blocks: list[tuple[int, bool]] = []
    for line in lines:
        while open_blocks and line.indent <= open_blocks[-1][0]:
            open_blocks.pop()
        if any(is_loop for _, is_loop in open_blocks):
            inside.add(line.lineno)
        if _BLOCK_HEADER.match(line.text):
            open_blocks.append((line.indent, _LOOP_HEADER.match(line.text) is not None))
    return inside


def _violations_in_lines(lines: list[CodeLine], label: str) -> list[str]:
    in_loop = _lines_inside_loops(lines)
    tainted = _elapsed_tainted_names(lines)
    found: list[str] = []

    for line in lines:
        asserted = _ASSERT.match(line.text)
        if asserted:
            test = asserted.group(1)
            if _contains_elapsed_delta(test):
                found.append(
                    f"{label}:{line.lineno}: asserting on a wall-clock elapsed delta "
                    f"(wait on a condition instead)"
                )
            else:
                for name in _NAME.findall(test):
                    if name in tainted:
                        found.append(
                            f"{label}:{line.lineno}: asserting on '{name}', a measured "
                            f"wall-clock elapsed time (wait on a condition instead)"
                        )
                        break
        for literal in _SLEEP.findall(line.text):
            value = float(literal)
            if 0 < value < _SLEEP_SENTINEL_MIN and line.lineno not in in_loop:
                found.append(
                    f"{label}:{line.lineno}: sleep({value:g}) used as synchronization "
                    f"outside a poll loop (wait on a condition instead)"
                )
    return found


def _violations_in_source(source: str) -> list[str]:
    return _violations_in_lines(code_lines(source), "<snippet>")


def _violations(path: Path) -> list[str]:
    return _violations_in_lines(code_lines_of(path), str(path))


@pytest.mark.parametrize(
    "snippet",
    [
        "def test_x():\n    assert time.monotonic() - t0 < 5\n",
        "def test_x():\n    elapsed = perf_counter() - t0\n    assert elapsed < 5\n",
        "def test_x():\n    e1 = time.time() - t0\n    e2 = e1\n    assert e2 < 5\n",
        "def test_x():\n    await asyncio.sleep(0.2)\n",
        "def test_x():\n    time.sleep(3)\n",
    ],
    ids=["inline-delta", "tainted-name", "aliased-taint", "async-sleep", "sync-sleep"],
)
def test_scanner_flags_each_wallclock_pattern(snippet: str) -> None:
    """Positive control: the real guard passes vacuously once the suite is
    clean, so a silently broken scanner would never be caught by it."""
    assert _violations_in_source(snippet), f"scanner failed to flag: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        "def test_x():\n    t0 = time.monotonic()\n    assert t0\n",
        "def test_x():\n    deadline = time.monotonic() + 30\n    assert deadline\n",
        "def test_x():\n    while not done:\n        await asyncio.sleep(0.05)\n",
        "def test_x():\n    await asyncio.sleep(3600)\n",
        "def test_x():\n    await asyncio.sleep(0)\n",
        'def test_x():\n    """assert monotonic() - t0 < 5"""\n',
    ],
    ids=["bare-timestamp", "poll-ceiling", "sleep-in-loop", "block-forever", "yield", "docstring"],
)
def test_scanner_ignores_legitimate_timing(snippet: str) -> None:
    """Negative control: an over-eager scanner would fail the whole suite, so
    the shapes the guard deliberately allows are pinned here."""
    assert not _violations_in_source(snippet), f"scanner false-positived on: {snippet!r}"


def test_no_wallclock_synchronization_in_tests() -> None:
    violations: list[str] = []
    for path in _scanned_files():
        violations.extend(_violations(path))
    assert not violations, (
        "Wall-clock synchronization found in tests (fix, do not suppress):\n"
        + "\n".join(violations)
    )
