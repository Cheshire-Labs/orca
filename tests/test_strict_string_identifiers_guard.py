"""CI guard: identifier-typed string fields cannot use empty-string placeholders,
coercions, or sentinel checks in production code.

A data-loss incident was rooted in ``thread_id=""`` placeholders used as
"fill in later" slots in the dispatcher / interpreter
chain. Empty records flowed into ops_history; search-by-thread filters became
silent no-ops; the entire liquid-handler call history was lost.

This guard catches every variant of the cheat pattern the audit found:

  1. ``*_id: <type> = ""`` argument defaults (direct literal)
  2. ``*_id: <type> = typer.Option("")`` / ``= Field("")`` (literal in a call)
  3. ``<expr>.thread_id or ""`` two-operand or chained (``a or b or ""``)
  4. ``<expr>.thread_id == ""`` / ``!= ""`` sentinel-rewrite checks
  5. ``not <expr>.thread_id`` (used as "is unset" check, same defect class)

Identifier suffix: argument or attribute names ending in ``_id``. Covers
``thread_id``, ``execution_id``, ``action_id``, ``method_id``, ``device_id``,
``labware_id``, ``operator_id``, ``parent_id``, etc.

Two rules for legitimate ``_id`` handling:
  - Truly Optional field (``str | None`` in the domain): keep the Optional all
    the way through; at display, convert with explicit
    ``x if x is not None else ""`` (a ternary, NOT an ``or`` chain).
  - Non-Optional field (``str`` in the domain): the ``or "..."`` is dead code;
    delete it.

Scope: ``src/orca/`` only. Tests are excluded; the guard runs on production code.

String literals arrive here blanked to their quotes plus spaces, so a
literal reads as ``""`` when empty and ``"   "` otherwise. That is what lets
these patterns tell "any string literal" from "the empty string" without
looking at what the literal said.
"""

import re
from pathlib import Path

import pytest

from cheshire_source_text import CodeLine, code_lines, code_lines_of, python_files


REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src"

_ID = r"[A-Za-z_]\w*_id"
_QUALIFIED_ID = rf"(?:[A-Za-z_]\w*\.)*{_ID}"
_STRING = r"""(?:"[^"]*"|'[^']*')"""
_EMPTY_STRING = r"""(?:""|'')"""

# `_id=` followed by a string literal, either bare or as the default slot of a
# wrapper call (`typer.Option("")`, `Field("")`, `Field(default="x")`).
_ARG_DEFAULT = re.compile(
    rf"\b({_ID})\s*(?::[^=,)]+?)?=\s*"
    rf"(?:{_STRING}|[A-Za-z_][\w.]*\(\s*(?:{_STRING}|[^)]*?\bdefault(?:_factory)?\s*=\s*{_STRING}))"
)
_OR_LITERAL = re.compile(rf"\b{_QUALIFIED_ID}\b(?:\s+or\s+[^\s]+)*\s+or\s+{_STRING}")
_EQ_EMPTY = re.compile(rf"\b({_QUALIFIED_ID})\s*(==|!=)\s*{_EMPTY_STRING}")
_TERNARY_LITERAL = re.compile(
    rf"\b{_QUALIFIED_ID}\b\s+if\s+.*?\bis\s+not\s+None\s+else\s+{_STRING}"
)
_NOT_ID = re.compile(rf"\bnot\s+({_QUALIFIED_ID})\b")
_DEF = re.compile(r"^(?:async\s+)?def\s+")

_CATEGORIES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("string-literal default", _ARG_DEFAULT, True),
    ("or <literal> coercion", _OR_LITERAL, False),
    ("empty-string comparison", _EQ_EMPTY, False),
    ("ternary identifier-to-literal coercion", _TERNARY_LITERAL, False),
    ("not <_id> falsy check", _NOT_ID, False),
)


def _scan_lines(lines: list[CodeLine]) -> list[tuple[int, str, str]]:
    """Return (lineno, category, snippet) for every cheat pattern in `lines`."""
    hits: list[tuple[int, str, str]] = []
    for line in lines:
        for category, pattern, defs_only in _CATEGORIES:
            if defs_only and not _DEF.match(line.text):
                continue
            for match in pattern.finditer(line.text):
                hits.append((line.lineno, category, match.group(0).strip()))
    return hits


def _scan_source(source: str) -> list[tuple[int, str, str]]:
    return _scan_lines(code_lines(source))


def _scan(path: Path) -> list[tuple[int, str, str]]:
    return _scan_lines(code_lines_of(path))


@pytest.mark.parametrize(
    "snippet",
    [
        'def f(thread_id: str = ""): ...',
        'def f(thread_id: str = typer.Option("")): ...',
        'def f(thread_id: str = Field(default="x")): ...',
        'x = rec.thread_id or ""',
        'x = a.thread_id or b.execution_id or ""',
        'x = rec.thread_id or "-"',
        'if rec.thread_id == "": ...',
        'if rec.thread_id != "": ...',
        'x = rec.thread_id if rec.thread_id is not None else ""',
        'if not rec.thread_id: ...',
    ],
    ids=[
        "arg-default",
        "arg-default-typer-option",
        "arg-default-field-keyword",
        "or-literal-two-operand",
        "or-literal-chained",
        "or-literal-magic-dash",
        "eq-empty-string",
        "neq-empty-string",
        "ternary-to-literal",
        "not-id",
    ],
)
def test_scanner_flags_each_cheat_category(snippet: str) -> None:
    """Positive control: prove the scanner actually fires.

    The per-file guard passes vacuously when production code is clean (it has no
    `*_id` cheats), so a silently broken scanner would never be caught by it.
    Each snippet here carries exactly one cheat and must produce a hit.
    """
    assert _scan_source(snippet), f"scanner failed to flag cheat: {snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "def f(thread_id: str = SYSTEM_ID): ...",
        "x = rec.thread_id or SYSTEM_ID",
        "y = '--thread-id'",
        "z = rec.thread_id",
        'parser.add_argument("--thread-id", help="the thread id")',
        'log.info("no thread_id on %s", rec)',
        "def f(name: str = \"\"): ...",
        "if rec.thread_id is None: ...",
    ],
    ids=[
        "named-constant-default",
        "named-constant-fallback",
        "flag-name-literal",
        "plain-read",
        "flag-registration",
        "id-mentioned-in-message",
        "non-id-empty-default",
        "explicit-is-none",
    ],
)
def test_scanner_ignores_legitimate_id_handling(snippet: str) -> None:
    """Negative control: real-value and Optional-via-ternary handling pass.

    Guards against an over-eager scanner that flags everything (which would also
    make the per-file guard meaningless by failing on clean code)."""
    hits = _scan_source(snippet)
    assert not hits, f"scanner flagged legitimate handling: {snippet!r} -> {hits}"


@pytest.mark.parametrize(
    "path",
    python_files(SRC_ROOT),
    ids=lambda p: str(p.relative_to(SRC_ROOT)),
)
def test_no_empty_string_identifier_cheats(path: Path) -> None:
    hits = _scan(path)
    if not hits:
        return
    rel = path.relative_to(SRC_ROOT)
    msg = [f"{rel} contains empty-string identifier cheats:"]
    for lineno, category, snippet in hits:
        msg.append(f"  line {lineno} [{category}]: {snippet}")
    msg.append("")
    msg.append(
        "Identifier-typed fields (`*_id`) must carry a real value. Two rules:\n"
        "  - Truly Optional field: keep the Optional all the way through; at "
        "display, convert with `x if x is not None else \"\"` (ternary, NOT "
        "`or` chain). Optional propagation is the whole point of Optional.\n"
        "  - Non-Optional field: any `or \"...\"` / `== \"\"` / `not x` is "
        "dead code or a lie about the type -- delete it or fix the type."
    )
    pytest.fail("\n".join(msg))
