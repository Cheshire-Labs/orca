"""Guard: example and documentation code imports from `orca.orca` and `orca.sdk.<topic>` only.

Read as text through `cheshire_source_text`. Covers `examples/` (Python files and code blocks in its
Markdown) and code blocks, Python or unlabelled, in the Markdown files at the repo root and in `docs/*.md`.
"""

import re
from pathlib import Path

from cheshire_source_text import CodeLine, code_lines, code_lines_of, imports_rooted_at, python_files

_ROOT = Path(__file__).resolve().parent.parent

_ALLOWED_MODULE = re.compile(r"^orca\.orca$|^orca\.sdk\.[A-Za-z_][A-Za-z_0-9]*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*(\S*)")


def _bad_imports(label: str, lines: list[CodeLine], first_line: int = 1) -> list[str]:
    return [
        f"{label}:{found.lineno + first_line - 1}: {found.module}"
        for found in imports_rooted_at(lines, frozenset({"orca"}))
        if not _ALLOWED_MODULE.match(found.module)
    ]


def _python_blocks(markdown: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    fence = ""
    wanted = False
    first = 0
    body: list[str] = []
    for number, line in enumerate(markdown.splitlines(), start=1):
        found = _FENCE.match(line)
        if not fence and found:
            fence, language = found.group(1), found.group(2).lower()
            wanted = language == "" or language.startswith("py")
            first, body = number + 1, []
        elif fence and found and found.group(1).startswith(fence) and not found.group(2):
            if wanted:
                blocks.append((first, "\n".join(body)))
            fence = ""
        elif fence:
            body.append(line)
    assert not fence, f"a code fence opened at line {first - 1} is never closed"
    return blocks


def _bad_imports_in_markdown(label: str, markdown: str) -> list[str]:
    found: list[str] = []
    for first, block in _python_blocks(markdown):
        found += _bad_imports(label, code_lines(block), first)
    return found


def _user_code_files() -> tuple[list[Path], list[Path]]:
    python = python_files(_ROOT / "examples")
    markdown = [
        *sorted((_ROOT / "examples").rglob("*.md")),
        *sorted(_ROOT.glob("*.md")),
        *sorted((_ROOT / "docs").glob("*.md")),
    ]
    return python, markdown


def test_user_code_imports_only_from_orca_orca_and_orca_sdk() -> None:
    python, markdown = _user_code_files()
    found: list[str] = []
    for path in python:
        found += _bad_imports(path.relative_to(_ROOT).as_posix(), code_lines_of(path))
    for path in markdown:
        found += _bad_imports_in_markdown(path.relative_to(_ROOT).as_posix(), path.read_text(encoding="utf-8"))
    assert found == [], (
        "user-style code imports from an internal `orca` module. Import from `orca.orca` or `orca.sdk.<topic>`; "
        "if the name is not exported there, add it to the matching `orca/sdk/<topic>.py` `__all__`:\n"
        + "\n".join(found)
    )


def test_the_scan_covers_the_examples_and_the_docs() -> None:
    python, markdown = _user_code_files()
    assert len(python) >= 10
    assert all(path.exists() for path in markdown)
    blocks = [block for path in markdown for block in _python_blocks(path.read_text(encoding="utf-8"))]
    assert any("orca.sdk." in body for _, body in blocks)


def test_the_guard_fires_on_an_internal_import() -> None:
    for source in (
        "from orca.devices.devices import Reader\n",
        "import orca.runtime.system_runtime\n",
        "from orca import runtime\n",
        "from orca.sdk import devices\n",
        "from orca.cli.daemon_client import DaemonLoadingClient\n",
        "from orca.workflow_models.action_context import (\n    ActionContext,\n)\n",
    ):
        assert len(_bad_imports("a.py", code_lines(source))) == 1, source


def test_the_guard_passes_the_sdk_and_the_authoring_module() -> None:
    for source in (
        "import orca.orca as orca\n",
        "from orca.sdk.devices import Reader\n",
        "from orca.sdk.workflow import (\n    ActionContext,\n)\n",
        "from cheshire_drivers.labware_interfaces import IPlate\n",
        "x = 'from orca.devices.devices import Reader'\n# from orca.runtime.x import Y\n",
    ):
        assert _bad_imports("a.py", code_lines(source)) == [], source


def test_the_guard_reports_the_line_of_a_bad_import_inside_a_markdown_block() -> None:
    markdown = "text\n\n```python\nimport orca.orca as orca\nfrom orca.devices.devices import Reader\n```\n"
    assert _bad_imports_in_markdown("a.md", markdown) == ["a.md:5: orca.devices.devices"]


def test_the_guard_ignores_an_import_outside_a_python_block() -> None:
    markdown = "```bash\nfrom orca.devices.devices import Reader\n```\n\nfrom orca.devices.devices import Reader\n"
    assert _bad_imports_in_markdown("a.md", markdown) == []


def test_the_guard_reads_every_fence_that_may_hold_python() -> None:
    bad = "from orca.devices.devices import Reader\n"
    for opening, closing in (
        ('```python title="x"', "```"),
        ("```python3", "```"),
        ("```Python", "```"),
        ("~~~python", "~~~"),
        ("````python", "````"),
        ("```", "```"),
    ):
        markdown = f"{opening}\n{bad}{closing}\n"
        assert _bad_imports_in_markdown("a.md", markdown) == ["a.md:2: orca.devices.devices"], opening
