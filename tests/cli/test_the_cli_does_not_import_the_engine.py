"""The CLI talks to the daemon over HTTP, so it must not load the engine.

It imports wire models to build request bodies and parse responses. Those
models used to live in the same modules as the code that runs workflows, so
`orca version` loaded the scheduler, the resource graph, pylabrobot and
matplotlib before printing one line. That cost about three seconds on every
invocation.

Each case starts a fresh interpreter, because `sys.modules` in the test
process is already full of whatever the suite imported.
"""

import subprocess
import sys

import pytest

# Loading any of these means a wire model is sitting in an engine module again.
ENGINE_MODULES = [
    "orca.runtime.system_runtime",
    "orca.runtime.runtime_interface",
    "orca.runtime.execution",
    "orca.system.system_interface",
    "orca.resource_models.labware",
    "orca.resource_models.labware_location_service",
]

THIRD_PARTY = ["pylabrobot", "matplotlib", "numpy"]

# `import orca.cli.app` loads about 80 orca modules, and 287 with the engine.
# The ceiling sits between: a new command fits, the engine does not.
MODULE_CEILING = 150


def _loaded_after_importing_the_cli() -> set[str]:
    probe = (
        "import sys, orca.cli.app; "
        "print('\\n'.join(sorted(sys.modules)))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    return set(done.stdout.split())


@pytest.fixture(scope="module")
def loaded() -> set[str]:
    return _loaded_after_importing_the_cli()


@pytest.mark.parametrize("module", ENGINE_MODULES)
def test_the_cli_does_not_load_an_engine_module(module: str, loaded: set[str]) -> None:
    assert module not in loaded


@pytest.mark.parametrize("package", THIRD_PARTY)
def test_the_cli_does_not_load_a_heavy_third_party_package(
    package: str, loaded: set[str]
) -> None:
    assert package not in loaded


def test_the_probe_actually_imported_the_cli(loaded: set[str]) -> None:
    """Without this, an empty or failed probe would make every case pass."""
    assert "orca.cli.app" in loaded
    assert "typer" in loaded


def test_the_cli_stays_under_its_module_budget(loaded: set[str]) -> None:
    orca_modules = sorted(m for m in loaded if m.startswith("orca."))
    assert len(orca_modules) < MODULE_CEILING, (
        f"{len(orca_modules)} orca modules loaded; the engine is probably back. "
        f"{orca_modules}"
    )


def test_every_verb_still_builds() -> None:
    """A verb whose module moved would fail here rather than at first use."""
    probe = (
        "from orca.cli.app import app\n"
        "def walk(node, path, found):\n"
        "    for command in node.registered_commands:\n"
        "        name = command.name or (\n"
        "            command.callback.__name__.replace('_', '-')\n"
        "            if command.callback else None)\n"
        "        if name:\n"
        "            found.append(' '.join(path + [name]))\n"
        "    for group in node.registered_groups:\n"
        "        if group.typer_instance is not None and group.name:\n"
        "            walk(group.typer_instance, path + [group.name], found)\n"
        "found = []\n"
        "walk(app, [], found)\n"
        "print(len(found))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert int(done.stdout.strip()) >= 135
