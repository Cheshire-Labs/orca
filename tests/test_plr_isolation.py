"""Guard: orca-core/src/ never imports pylabrobot directly, and orca-core's
pyproject.toml does not declare pylabrobot as a dependency.

PLR is a transitive dependency of cheshire-drivers only. orca-core gets
PLR types via `cheshire_drivers.plr` re-exports.

Two regressions this guard catches:
  1. A new `from pylabrobot... import` slipping into orca-core/src/.
  2. Someone re-adding `pylabrobot @ git+...` to pyproject.toml.
"""

from pathlib import Path

import pytest

from cheshire_source_text import CodeLine, code_lines, code_lines_of, imports_rooted_at, python_files


REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"

_PLR_ROOT = frozenset({"pylabrobot"})


def _direct_plr_imports_in_lines(lines: list[CodeLine]) -> list[str]:
    """Return rendered import lines that pull pylabrobot directly."""
    return [found.rendered() for found in imports_rooted_at(lines, _PLR_ROOT)]


def _direct_plr_imports_in_source(source: str) -> list[str]:
    return _direct_plr_imports_in_lines(code_lines(source))


def _direct_plr_imports(path: Path) -> list[str]:
    return _direct_plr_imports_in_lines(code_lines_of(path))


@pytest.mark.parametrize("path", python_files(SRC_ROOT), ids=lambda p: str(p.relative_to(SRC_ROOT)))
def test_no_direct_pylabrobot_imports(path: Path) -> None:
    hits = _direct_plr_imports(path)
    assert not hits, (
        f"{path.relative_to(SRC_ROOT)} imports pylabrobot directly:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nRoute the import through cheshire_drivers.plr instead "
        + "(re-export the symbol there if it is not already exposed)."
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "import pylabrobot",
        "import pylabrobot.resources",
        "import pylabrobot as plr",
        "import os, pylabrobot",
        "from pylabrobot import resources",
        "from pylabrobot.liquid_handling import LiquidHandler",
    ],
)
def test_scanner_flags_direct_pylabrobot_import(snippet: str) -> None:
    """Positive control: the per-file guard passes vacuously on clean source,
    so a silently broken scanner would never be caught by it. Each snippet
    carries one direct pylabrobot import and must produce a hit."""
    hits = _direct_plr_imports_in_source(snippet)
    assert hits, f"scanner failed to flag direct import: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        "from cheshire_drivers.plr import LiquidHandler",
        "import cheshire_drivers.plr as plr",
        "from orca.devices.devices import LiquidHandler",
        "x = 'pylabrobot'",
        "import pylabrobotics",
        "from pylabrobotics import thing",
        "# import pylabrobot",
    ],
)
def test_scanner_ignores_legitimate_imports(snippet: str) -> None:
    """Negative control: re-exported (`cheshire_drivers.plr`) and unrelated
    imports, a string literal, a comment, and a same-prefix package
    (`pylabrobotics`) must not be flagged. Guards against an over-eager scanner
    that would also make the per-file guard meaningless by failing on clean
    code."""
    hits = _direct_plr_imports_in_source(snippet)
    assert not hits, f"scanner false-positived on: {snippet!r} -> {hits}"


def test_pyproject_does_not_declare_pylabrobot() -> None:
    """pylabrobot must not appear as a direct dependency of orca-core.

    cheshire-drivers brings pylabrobot transitively; declaring it again here
    forces version coordination across repos, which this guard exists to
    prevent.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    offenders = [
        line for line in text.splitlines()
        if "pylabrobot" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "pyproject.toml mentions pylabrobot in non-comment lines:\n"
        + "\n".join(f"  {line}" for line in offenders)
        + "\n\npylabrobot is owned by cheshire-drivers; remove the direct dep."
    )
