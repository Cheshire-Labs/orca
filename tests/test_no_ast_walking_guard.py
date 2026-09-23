"""Guard: nothing in this repo parses source into a syntax tree and walks it.

No `ast.parse` plus node dispatch, no `NodeVisitor`, no whitelist node walkers,
no AST-based validators, no AST-based expression evaluators. Outside `tests/`
and `bench/` this is not a style preference and it has no opt-out: every
previous walker in the shipped code was a hand-rolled parser or interpreter over
Python's grammar, and each one cost more to maintain than the thing it claimed
to buy. Two of them claimed to be safety boundaries and provably were not.

`eval` and `exec` are fine. The ban is on hand-rolling an interpreter over the
syntax tree, never on executing a string.

Why `tests/` and `bench/` are the only exemptions. The ban aims at two shipped
uses: a syntax tree standing in for a validity or security check over code a
user submitted, and a syntax tree used to discover domain entities out of
function bodies someone else wrote. Neither exists in a CI guard or a bench
harness. There is no adversary, the input is our own tree, and a miss costs
undetected drift rather than a breach. Judgement is a bad fence here, because
"this one isn't a security check" is exactly what gets said in the moment of
temptation, so the line is drawn by directory instead of by argument. Everything
else the repo ships or runs -- packages, examples, scripts, migrations,
deployment templates -- is inside the ban.

What to do instead:

* Enforcing a rule about source? Read it as text. `cheshire_source_text` blanks
  strings and comments and joins bracket continuations, which is what makes a
  line pattern reliable. Every guard in this repo is written that way.
* Need a condition, formula, or threshold from a config file? Use structured
  data with typed fields: an enum, a predicate as data
  (`{"field": ..., "op": "gt", "value": ...}`), or a typed object the caller
  constructs. If that feels impossible, `eval` against a namespace with no
  builtins is the sanctioned escape hatch.
* Restricting what submitted code may do? Restrict it where it runs, not where
  it parses: a namespace whose builtins are stubs that raise, and an
  `__import__` that checks the module. Runtime checks catch the indirect reach
  a parse-time allow-list never could.

If a design seems to need a node-walking interpreter, the model is wrong, not
the parser.
"""

from pathlib import Path

import pytest

from cheshire_source_text import (
    code_lines,
    code_lines_of,
    imports_rooted_at,
    python_files,
)

REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src"

_EXEMPT_TREES = frozenset({"tests", "bench"})

# `ast` and `_ast` are the standard library's syntax tree. The rest are
# third-party trees that would be the same move under a different name.
_TREE_MODULES = frozenset({
    "ast",
    "_ast",
    "symtable",
    "lib2to3",
    "typed_ast",
    "libcst",
    "parso",
    "redbaron",
    "asttokens",
})


def _scanned_files() -> list[Path]:
    """The whole repo apart from `tests/` and `bench/`.

    Fail-closed: a directory added later is covered the day it lands rather than
    the day someone remembers to add it here.
    """
    return [
        path for path in python_files(REPO_ROOT)
        if path.relative_to(REPO_ROOT).parts[0] not in _EXEMPT_TREES
    ]


def _tree_imports_in_source(source: str) -> list[str]:
    return [found.rendered() for found in imports_rooted_at(code_lines(source), _TREE_MODULES)]


def _tree_imports(path: Path) -> list[str]:
    return [found.rendered() for found in imports_rooted_at(code_lines_of(path), _TREE_MODULES)]


@pytest.mark.parametrize(
    "snippet",
    [
        "import ast",
        "import ast, re",
        "import ast as syntax",
        "from ast import parse",
        "from ast import NodeVisitor, walk",
        "import libcst",
        "from lib2to3 import refactor",
    ],
)
def test_guard_flags_a_syntax_tree_import(snippet: str) -> None:
    """Positive control: the guard passes vacuously while the tree is clean, so
    a silently broken scanner would never be caught by it."""
    assert _tree_imports_in_source(snippet), f"guard failed to flag: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        "import asttokens_helper",
        "from astropy import units",
        "x = 'import ast'",
        "# import ast",
        "from dataclasses import dataclass",
    ],
)
def test_guard_ignores_unrelated_imports(snippet: str) -> None:
    """Negative control: an over-eager guard would fail the whole repo. A
    same-prefix package, a string, and a comment are not syntax trees."""
    assert not _tree_imports_in_source(snippet), f"guard false-positived on: {snippet!r}"


def test_the_scan_covers_the_repo_and_exempts_only_tests_and_bench() -> None:
    """Scope control: an empty, misrooted, or over-exempting file list would
    leave the ban passing on every commit while checking nothing."""
    scanned = _scanned_files()
    assert scanned, f"no Python files found under {REPO_ROOT}"
    assert any(SRC_ROOT in path.parents for path in scanned), (
        f"the scan reached no shipped code under {SRC_ROOT}"
    )
    assert Path(__file__) not in scanned, (
        "this guard sits under tests/, which is outside the ban, so the scan must not reach it"
    )


@pytest.mark.parametrize("path", _scanned_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_shipped_module_imports_a_syntax_tree(path: Path) -> None:
    hits = _tree_imports(path)
    assert not hits, (
        f"{path.relative_to(REPO_ROOT)} imports a syntax tree:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nParsing source into a tree and walking it is banned everywhere "
        "except tests/ and bench/. Read the source as text via "
        "cheshire_source_text, model the rule as typed structured data, or "
        "enforce it at runtime. See this module's docstring."
    )
