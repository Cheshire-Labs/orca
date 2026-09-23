"""Verb -> method backend-dispatch parity guard.

The bug this prevents: a CLI verb whose underlying operation exists on
`IControlPlaneClient` (so both the local daemon AND the cloud backend
implement it, with wire routes pinned in `test_command_parity.ROUTE_CATALOG`)
gets its client via `local_client()`. That hard-gates the verb to the local
daemon and makes it fail-clean on `--backend cloud`, even though the cloud
backend can serve it. `audit list` and the seven `execution thread` mutation
verbs shipped that way.

The guard is structural, not a per-verb allowlist, so it also covers verbs
added later. For every Typer command in the CLI verb modules it collects the
client methods the command calls on a `local_client()`-derived object. If that
set is non-empty and every method in it is declared on `IControlPlaneClient`,
the command has no local-only reason to gate `local`: it must use
`get_client()` (active backend) instead. A command that also calls at least one
local-only method (`spawn_thread`, `recover_thread`, `pause_thread`, ...) is
legitimately local and not flagged.
"""

import inspect
import re
from pathlib import Path

import pytest

# Import the fully-wired app module (not the verb submodules directly) to
# avoid the app<->verb circular import during collection.
from orca.cli import app as _app_mod
from orca.cli.control_plane import IControlPlaneClient

from cheshire_source_text import CodeLine, code_lines, code_lines_of


_CLI_DIR = Path(inspect.getfile(_app_mod)).parent

_COMMAND_DECORATOR = re.compile(r"^@\w+\.(?:command|callback)\b")
_DEF = re.compile(r"^(?:async\s+)?def\s+(\w+)")
_LOCAL_ASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=]+?)?=\s*local_client\s*\(\s*\)")
_LOCAL_CHAINED = re.compile(r"\blocal_client\s*\(\s*\)\s*\.(\w+)\s*\(")


def _protocol_method_names() -> frozenset[str]:
    return frozenset(
        name
        for name, value in inspect.getmembers(IControlPlaneClient)
        if not name.startswith("_") and inspect.isfunction(value)
    )


def _local_methods_called(body: list[CodeLine]) -> set[str]:
    """Method names invoked on a `local_client()`-derived object inside `body`.

    Tracks both the assigned form (`c = local_client(); c.foo()`) and the
    chained form (`local_client().foo()`).
    """
    local_names: set[str] = set()
    for line in body:
        assigned = _LOCAL_ASSIGN.match(line.text)
        if assigned:
            local_names.add(assigned.group(1))

    called: set[str] = set()
    for line in body:
        called.update(_LOCAL_CHAINED.findall(line.text))
        for name in local_names:
            called.update(
                re.findall(rf"(?<!\w){re.escape(name)}\.(\w+)\s*\(", line.text)
            )
    return called


def _command_bodies(lines: list[CodeLine]) -> list[tuple[str, list[CodeLine]]]:
    """(function name, body lines) for every `@<app>.command/callback` function."""
    commands: list[tuple[str, list[CodeLine]]] = []
    decorated = False
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.text.startswith("@"):
            decorated = decorated or _COMMAND_DECORATOR.match(line.text) is not None
            continue
        named = _DEF.match(line.text)
        if named is None:
            decorated = False
            continue
        start = index
        while index < len(lines) and lines[index].indent > line.indent:
            index += 1
        if decorated:
            commands.append((named.group(1), lines[start:index]))
        decorated = False
    return commands


def _command_bodies_of(module_path: Path) -> list[tuple[str, list[CodeLine]]]:
    return _command_bodies(code_lines_of(module_path))


# Verbs whose underlying op IS on IControlPlaneClient but whose cloud/local
# return types diverge (different DTO classes for the same op), so they cannot
# yet dispatch via get_client() without DTO reconciliation. Remove each
# entry when its DTO is unified.
_KNOWN_DTO_DIVERGENT: frozenset[tuple[str, str]] = frozenset({
    ("describe.py", "describe_device"),       # device_info: DaemonDeviceDTO vs control_plane.DeviceDTO
    ("execution.py", "list_threads_cmd"),     # cloud get_execution().threads vs local list_threads()
})


def _mis_wired_commands() -> list[str]:
    protocol = _protocol_method_names()
    offenders: list[str] = []
    for module_path in sorted(_CLI_DIR.glob("*.py")):
        if module_path.name in ("__init__.py", "backend.py"):
            continue
        for name, body in _command_bodies_of(module_path):
            if (module_path.name, name) in _KNOWN_DTO_DIVERGENT:
                continue
            called = _local_methods_called(body)
            if called and called <= protocol:
                offenders.append(
                    f"{module_path.name}:{name} routes {sorted(called)} "
                    f"through local_client(); these are IControlPlaneClient "
                    f"methods, so the verb must use get_client()"
                )
    return offenders


def test_no_protocol_only_verb_is_gated_local() -> None:
    """No CLI verb may serve an IControlPlaneClient-only op via local_client()."""
    offenders = _mis_wired_commands()
    assert not offenders, "backend-dispatch mis-wiring:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "src",
    [
        (
            "@app.command('x')\n"
            "def bad():\n"
            "    client = local_client()\n"
            "    client.audit_list()\n"
        ),
        (
            "@app.command('x')\n"
            "def bad():\n"
            "    local_client().audit_list()\n"
        ),
    ],
    ids=["assigned", "chained"],
)
def test_guard_detects_a_synthetic_mis_wiring(src: str) -> None:
    """The guard actually fires on the pattern it claims to catch.

    Without this, a refactor that silently broke `_local_methods_called`
    (e.g. stopped tracking the chained form) would make the real guard pass
    vacuously. `audit_list` is on IControlPlaneClient, so a synthetic command
    that calls it through `local_client()` must be flagged.
    """
    commands = _command_bodies(code_lines(src))
    assert [name for name, _ in commands] == ["bad"]
    called = _local_methods_called(commands[0][1])
    assert called == {"audit_list"}
    assert called <= _protocol_method_names()


def test_guard_ignores_a_get_client_verb() -> None:
    """Negative control: a verb on the active backend must not be flagged, or
    the guard would fail every correctly-wired command."""
    src = (
        "@app.command('x')\n"
        "def fine():\n"
        "    client = get_client()\n"
        "    client.audit_list()\n"
    )
    commands = _command_bodies(code_lines(src))
    assert _local_methods_called(commands[0][1]) == set()


def test_guard_ignores_an_undecorated_function() -> None:
    """Negative control: only Typer commands are in scope; a plain helper that
    uses local_client() is not a dispatch decision."""
    src = (
        "def helper():\n"
        "    local_client().audit_list()\n"
    )
    assert _command_bodies(code_lines(src)) == []


def test_audit_and_thread_mutations_are_not_gated_local() -> None:
    """The eight verbs this guard was written for are clean post-fix."""
    audit_cmds = {name for name, _ in _command_bodies_of(_CLI_DIR / "audit.py")}
    assert "audit_list" in audit_cmds
    offenders = _mis_wired_commands()
    assert not offenders
