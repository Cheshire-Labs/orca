"""Guard: the layer order, one rule added each time the code has no violation of it left.

Read as text through `cheshire_source_text`, which blanks strings and comments and joins bracket continuations.

- No `orca` import sits inside a function or class in `workflow_models/`, `system/` or `resource_models/`.
- Nothing in `resource_models/`, `devices/`, `state/` or `driver_management/` imports the engine, the system, the
  operations, the API, the CLI, the daemon or the SDK.
"""

import re
from collections.abc import Callable
from pathlib import Path

from cheshire_source_text import CodeLine, code_lines, code_lines_of, python_files

_SRC = Path(__file__).resolve().parent.parent / "src" / "orca"

_IMPORTS_ORCA = re.compile(r"^(?:from\s+orca\b|import\s+orca\b)")
_NO_INDENTED_IMPORTS_IN = ("workflow_models/", "system/", "resource_models/")

_RESOURCE_LAYER = ("resource_models/", "devices/", "state/", "driver_management/")
_HIGHER_PACKAGES = "workflow_models|system|operations|api|cli|daemon|sdk"
_IMPORTS_A_HIGHER_PACKAGE = re.compile(
    rf"^(?:from\s+orca\.(?:{_HIGHER_PACKAGES})\b|import\s+.*\borca\.(?:{_HIGHER_PACKAGES})\b"
    rf"|from\s+orca\s+import\s+.*\b(?:{_HIGHER_PACKAGES})\b)"
)

Rule = Callable[[str, list[CodeLine]], list[str]]


def _indented_imports(relative: str, lines: list[CodeLine]) -> list[str]:
    if not relative.startswith(_NO_INDENTED_IMPORTS_IN):
        return []
    return [f"{relative}:{line.lineno}: {line.text}" for line in lines if line.indent > 0 and _IMPORTS_ORCA.match(line.text)]


def _imports_of_a_higher_layer(relative: str, lines: list[CodeLine]) -> list[str]:
    if not relative.startswith(_RESOURCE_LAYER):
        return []
    return [f"{relative}:{line.lineno}: {line.text}" for line in lines if _IMPORTS_A_HIGHER_PACKAGE.match(line.text)]


def _scan_the_tree(rule: Rule) -> list[str]:
    found: list[str] = []
    for path in python_files(_SRC):
        found += rule(path.relative_to(_SRC).as_posix(), code_lines_of(path))
    return found


def test_no_orca_import_is_written_inside_a_function_in_the_engine_system_or_resources() -> None:
    found = _scan_the_tree(_indented_imports)
    assert found == [], (
        "an `orca` import inside a function or class in workflow_models/, system/ or resource_models/. "
        "Move it to the top of the file; if that makes a cycle, move the name it imports down a layer:\n"
        + "\n".join(found)
    )


def test_the_resource_layer_imports_nothing_above_it() -> None:
    found = _scan_the_tree(_imports_of_a_higher_layer)
    assert found == [], (
        "a file in resource_models/, devices/, state/ or driver_management/ imports a higher layer. "
        "Move the name it needs down, or the code that needs it up:\n" + "\n".join(found)
    )


def test_every_folder_the_rules_name_holds_python_files() -> None:
    for folder in (*_NO_INDENTED_IMPORTS_IN, *_RESOURCE_LAYER):
        assert any((_SRC / folder).rglob("*.py")), folder


def test_the_indented_import_rule_fires_on_an_import_inside_a_function() -> None:
    source = "def f():\n    from orca.runtime.x import Y\n    return Y\n"
    assert len(_indented_imports("workflow_models/a.py", code_lines(source))) == 1
    assert len(_indented_imports("system/a.py", code_lines("class C:\n    import orca.foo\n"))) == 1
    assert len(_indented_imports("resource_models/a.py", code_lines("if x:\n    from orca import y\n"))) == 1


def test_the_indented_import_rule_fires_on_a_bracketed_import_inside_a_function() -> None:
    source = "def f():\n    from orca.runtime.x import (\n        Y,\n        Z,\n    )\n"
    assert len(_indented_imports("system/a.py", code_lines(source))) == 1


def test_the_indented_import_rule_passes_a_top_level_import_and_text_that_only_mentions_one() -> None:
    assert _indented_imports("workflow_models/a.py", code_lines("from orca.runtime.x import Y\n")) == []
    quoted = 'def f():\n    """\n    from orca.runtime.x import Y\n    """\n    # from orca.runtime.x import Y\n'
    assert _indented_imports("workflow_models/a.py", code_lines(quoted)) == []
    assert _indented_imports("workflow_models/a.py", code_lines("def f():\n    from other import orca_thing\n")) == []


def test_the_indented_import_rule_ignores_packages_it_does_not_cover() -> None:
    source = "def f():\n    from orca.runtime.x import Y\n"
    assert _indented_imports("runtime/a.py", code_lines(source)) == []


def test_the_higher_layer_rule_fires_on_each_package_it_names() -> None:
    for package in ("workflow_models", "system", "operations", "api", "cli", "daemon", "sdk"):
        source = f"from orca.{package}.thing import Name\n"
        assert len(_imports_of_a_higher_layer("resource_models/a.py", code_lines(source))) == 1, package
    assert len(_imports_of_a_higher_layer("devices/a.py", code_lines("import orca.system.system\n"))) == 1
    assert len(_imports_of_a_higher_layer("state/a.py", code_lines("from orca import sdk\n"))) == 1
    assert len(_imports_of_a_higher_layer("driver_management/a.py", code_lines("from orca import (\n    cli,\n)\n"))) == 1
    assert len(_imports_of_a_higher_layer("state/a.py", code_lines("from orca import config, sdk\n"))) == 1
    assert len(_imports_of_a_higher_layer("state/a.py", code_lines("import orca.config, orca.sdk\n"))) == 1
    indented = "def f():\n    from orca.operations.x import Y\n"
    assert len(_imports_of_a_higher_layer("state/a.py", code_lines(indented))) == 1


def test_the_higher_layer_rule_passes_an_import_of_its_own_layer_or_below() -> None:
    for source in (
        "from orca.resource_models.labware import LabwareInstance\n",
        "from orca.events.event_bus import EventBus\n",
        "from orca.systems_of_units import Name\n",
        "from orca.api_keys import Name\n",
        "from orca import config\n",
    ):
        assert _imports_of_a_higher_layer("resource_models/a.py", code_lines(source)) == [], source


def test_the_higher_layer_rule_ignores_a_package_it_does_not_cover() -> None:
    assert _imports_of_a_higher_layer("workflow_models/a.py", code_lines("from orca.system.x import Y\n")) == []
    assert _imports_of_a_higher_layer("runtime/a.py", code_lines("from orca.operations.x import Y\n")) == []
