"""The design rules that can be read off the code fail here, not in review."""

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import orca
import orca.operations
import pytest
from cheshire_source_text import code_lines
from pydantic import BaseModel, ConfigDict, RootModel

from orca.operations.typed_device_commands import TYPED_COMMAND_OPERATIONS

_SRC = Path(orca.__file__).resolve().parent

# A new package here is a decision about what orca-core is. Adding a
# name to this set is that decision, in a diff the owner reads.
_PACKAGES_IN_ORCA_CORE = {
    "api", "cli", "daemon", "devices", "driver_management", "events", "gateway", "modes",
    "operations", "plugins", "resource_models", "runtime", "sdk", "spawn", "state",
    "system", "variables", "workflow_models",
}

_IMPORTS_THE_DAEMON = re.compile(r"^\s*(?:from|import)\s+orca\.daemon\b")
_QUOTED_ANNOTATION = re.compile(
    r'(?:->\s*"[A-Z]|(?:^|[(,]\s*)\w+: "[A-Z]\w*"\s*(?:[,=)]|$)'
    r'|\b(?:list|Iterable|Awaitable|Sequence|Optional)\["[A-Z])'
)


# The labware Service is the only holder of a labware Store; a concrete Store is named
# only where a server chooses it at startup. The store contract checks the interface.
_LABWARE_STORE_NAMES_ALLOWED_IN = {
    "ILabwareStore": {
        "resource_models/store_interfaces.py", "runtime/labware_service.py", "runtime/startup.py", "store_contract.py",
    },
    "InMemoryLabwareStore": {"runtime/labware_store.py", "runtime/startup.py"},
    "SqlLabwareStore": {"runtime/sql_labware_store.py", "daemon/activity_database.py"},
}
_NAMES_A_LABWARE_STORE = re.compile(r"\b(ILabwareStore|InMemoryLabwareStore|SqlLabwareStore)\b")


def _operations() -> list[type]:
    found: list[type] = list(TYPED_COMMAND_OPERATIONS)
    for info in pkgutil.iter_modules(orca.operations.__path__):
        module = importlib.import_module(f"orca.operations.{info.name}")
        found.extend(
            value for value in vars(module).values()
            if inspect.isclass(value)
            and value.__module__ == module.__name__
            and value.__name__.endswith("Operation")
            and hasattr(value, "run")
            and hasattr(value, "Request")
        )
    return found


def _inherits_shared_code(op_class: type) -> bool:
    return op_class.__bases__ != (object,)


def _is_not_a_frozen_strict_model(model: type[BaseModel]) -> bool:
    config = model.model_config
    if issubclass(model, RootModel):
        return not config.get("frozen")
    return not (config.get("frozen") and config.get("extra") == "forbid")


def _lines_matching(pattern: re.Pattern[str], source: str) -> list[int]:
    return [line.lineno for line in code_lines(source) if pattern.search(line.text)]


def _quoted_annotations(source: str) -> list[int]:
    return [
        line.lineno for line in code_lines(source, keep_strings=True)
        if _QUOTED_ANNOTATION.search(line.text)
    ]


def test_orca_core_holds_only_the_packages_a_local_install_uses() -> None:
    packages = {p.name for p in _SRC.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert packages == _PACKAGES_IN_ORCA_CORE, (
        "A package was added to or removed from orca-core. "
        "Code only a hosted server runs does not belong here."
    )


@pytest.mark.parametrize("package", ["operations", "api"])
def test_operations_and_the_api_do_not_import_the_daemon(package: str) -> None:
    offenders = [
        f"{path.name}:{n}" for path in (_SRC / package).rglob("*.py")
        for n in _lines_matching(_IMPORTS_THE_DAEMON, path.read_text(encoding="utf-8"))
    ]
    assert not offenders


def test_the_daemon_import_scan_fires_on_an_import_and_not_on_a_mention() -> None:
    assert _lines_matching(_IMPORTS_THE_DAEMON, "from orca.daemon.app import create_app\n") == [1]
    assert _lines_matching(_IMPORTS_THE_DAEMON, "# from orca.daemon.app import x\n") == []


def test_no_operation_inherits_shared_code() -> None:
    assert not [op.__name__ for op in _operations() if _inherits_shared_code(op)]


def test_the_inheritance_check_tells_a_subclass_from_a_plain_class() -> None:
    class Plain: ...
    class Child(Plain): ...

    assert not _inherits_shared_code(Plain)
    assert _inherits_shared_code(Child)


def test_every_request_and_response_is_frozen_and_refuses_extra_fields() -> None:
    assert not sorted({
        model.__name__ for op in _operations()
        for model in (op.Request, op.Response)
        if _is_not_a_frozen_strict_model(model)
    })


def test_the_model_check_tells_a_strict_model_from_a_loose_one() -> None:
    class Strict(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

    class Loose(BaseModel):
        pass

    class FrozenRoot(RootModel[list[int]]):
        model_config = ConfigDict(frozen=True)

    assert not _is_not_a_frozen_strict_model(Strict)
    assert not _is_not_a_frozen_strict_model(FrozenRoot)
    assert _is_not_a_frozen_strict_model(Loose)


def _has_em_dash(text: str) -> bool:
    return chr(0x2014) in text


def test_source_has_no_em_dashes() -> None:
    assert not [
        path.name for path in _SRC.rglob("*.py") if _has_em_dash(path.read_text(encoding="utf-8"))
    ]


def test_the_em_dash_scan_fires_on_an_em_dash_and_not_on_a_hyphen() -> None:
    assert _has_em_dash(f"# one {chr(0x2014)} two\n")
    assert not _has_em_dash("# one -- two - three\n")


@pytest.mark.parametrize("package", ["operations", "api"])
def test_operations_and_the_api_have_no_quoted_annotations(package: str) -> None:
    offenders = [
        f"{path.name}:{n}" for path in (_SRC / package).rglob("*.py")
        for n in _quoted_annotations(path.read_text(encoding="utf-8"))
    ]
    assert not offenders


def _labware_store_names_out_of_place(module: str, source: str) -> list[str]:
    return [
        f"{module}:{line.lineno} {name}"
        for line in code_lines(source)
        for name in _NAMES_A_LABWARE_STORE.findall(line.text)
        if module not in _LABWARE_STORE_NAMES_ALLOWED_IN[name]
    ]


def test_only_the_labware_service_and_startup_name_a_labware_store() -> None:
    offenders = [
        found for path in _SRC.rglob("*.py")
        for found in _labware_store_names_out_of_place(
            path.relative_to(_SRC).as_posix(), path.read_text(encoding="utf-8"),
        )
    ]
    assert not offenders, "Hold ILabwareService; only startup code names a concrete store."


def test_the_labware_store_scan_fires_on_code_and_not_on_a_docstring() -> None:
    held = "from orca.resource_models.store_interfaces import ILabwareStore\n"
    assert _labware_store_names_out_of_place("runtime/system_runtime.py", held) == [
        "runtime/system_runtime.py:1 ILabwareStore",
    ]
    assert _labware_store_names_out_of_place("runtime/labware_service.py", held) == []
    mentioned = '"""Rows come from the ILabwareStore behind the service."""\n'
    assert _labware_store_names_out_of_place("runtime/system_runtime.py", mentioned) == []


def test_the_quoted_annotation_scan_fires_on_an_annotation_and_not_on_a_dict() -> None:
    assert _quoted_annotations('def f() -> "Thing":\n') == [1]
    assert _quoted_annotations('    outcome: "Thing",\n') == [1]
    assert _quoted_annotations('    "key": "Thing",\n') == []
    assert _quoted_annotations('class C:\n    x: "Thing"\n') == [2]
    assert _quoted_annotations('    x: list["Thing"]\n') == [1]
    assert _quoted_annotations('row["Thing"]\n') == []
    assert _quoted_annotations('def f(\n    a: int,\n    b: "Thing",\n) -> None:\n') == [1]


# Both servers and the test fixture build the runtime through orca.runtime.startup.
_CONSTRUCTS_A_RUNTIME = re.compile(r"\bSystemRuntime\(")
_THE_ONE_PLACE_A_RUNTIME_IS_BUILT = "runtime/startup.py"


def _runtime_constructions_out_of_place(module: str, source: str) -> list[str]:
    if module == _THE_ONE_PLACE_A_RUNTIME_IS_BUILT:
        return []
    return [f"{module}:{n}" for n in _lines_matching(_CONSTRUCTS_A_RUNTIME, source)]


def test_only_startup_constructs_a_system_runtime() -> None:
    offenders = [
        found for path in _SRC.rglob("*.py")
        for found in _runtime_constructions_out_of_place(
            path.relative_to(_SRC).as_posix(), path.read_text(encoding="utf-8"),
        )
    ]
    assert not offenders, "Build the runtime with orca.runtime.startup.start_runtime."


def test_the_runtime_construction_scan_fires_on_a_call_and_not_on_a_mention() -> None:
    built = "rt = SystemRuntime(\n    system,\n)\n"
    assert _runtime_constructions_out_of_place("runtime/runtime_holder.py", built) == [
        "runtime/runtime_holder.py:1",
    ]
    assert _runtime_constructions_out_of_place("runtime/startup.py", built) == []
    assert _runtime_constructions_out_of_place(
        "runtime/runtime_holder.py", '"""Calls SystemRuntime(...) through startup."""\n# SystemRuntime(x)\n',
    ) == []
    assert _runtime_constructions_out_of_place(
        "runtime/runtime_interface.py", "class ISystemRuntime(Protocol):\n",
    ) == []


# The gateway, the device factory, the activity services and the log writers are built in startup.
_CONSTRUCTS_A_GATEWAY_PART = re.compile(
    r"(?<!class )\b(DeviceConnectionTracker|ConnectionManager|ConnectionEventBus|DeviceController|DisconnectGrace"
    r"|DeviceCommandClock|DeviceAssignments|DeviceConnectionSource|RemoteDeviceFactory"
    r"|LabwareService|IncidentService|ExecutionRecordService|AuditWriter|EventWriter)\("
)
_THE_ONE_PLACE_A_GATEWAY_IS_BUILT = "runtime/startup.py"


def _gateway_parts_built_out_of_place(module: str, source: str) -> list[str]:
    if module == _THE_ONE_PLACE_A_GATEWAY_IS_BUILT:
        return []
    return [f"{module}:{n}" for n in _lines_matching(_CONSTRUCTS_A_GATEWAY_PART, source)]


def test_only_startup_constructs_a_gateway_part_or_an_activity_service() -> None:
    offenders = [
        found for path in _SRC.rglob("*.py")
        for found in _gateway_parts_built_out_of_place(
            path.relative_to(_SRC).as_posix(), path.read_text(encoding="utf-8"),
        )
    ]
    assert not offenders, "Build these in orca.runtime.startup and pass them in."


@pytest.mark.parametrize("name", [
    "DeviceConnectionTracker", "ConnectionManager", "ConnectionEventBus", "DeviceController", "DisconnectGrace",
    "DeviceCommandClock", "DeviceAssignments", "DeviceConnectionSource", "RemoteDeviceFactory",
    "LabwareService", "IncidentService", "ExecutionRecordService", "AuditWriter", "EventWriter",
])
def test_the_gateway_part_scan_fires_on_each_class_it_guards(name: str) -> None:
    assert _gateway_parts_built_out_of_place("daemon/app.py", f"part = {name}(x)\n") == ["daemon/app.py:1"]


def test_the_gateway_part_scan_fires_on_a_call_and_not_on_a_mention() -> None:
    built = "controller = DeviceController(tracker=t, connections=c)\n"
    assert _gateway_parts_built_out_of_place("daemon/app.py", built) == ["daemon/app.py:1"]
    assert _gateway_parts_built_out_of_place(
        "gateway/connection_handling.py", "grace = DisconnectGrace()\n",
    ) == ["gateway/connection_handling.py:1"]
    assert _gateway_parts_built_out_of_place(
        "daemon/activity_database.py", "self.labware = LabwareService(store)\n",
    ) == ["daemon/activity_database.py:1"]
    assert _gateway_parts_built_out_of_place("runtime/startup.py", built) == []
    assert _gateway_parts_built_out_of_place(
        "daemon/app.py", '"""Holds a ConnectionManager() built at startup."""\n# DeviceController()\n',
    ) == []
    assert _gateway_parts_built_out_of_place(
        "gateway/controller/controller.py", "class DeviceController:\nclass AuditWriter(LogStoreWriter):\n",
    ) == []


# A driver's pick and place are reached only inside a transporter's reach-in lease,
# which a run's move and an operator's command both take through Transporter.pick and place.
_REACHES_A_DRIVER_PICK_OR_PLACE = re.compile(r"\.(pick_at_coords|place_at_coords)\(|\breaching_in\(|\.reach_into\(")
_MAY_REACH_A_DRIVER_PICK_OR_PLACE = {
    "resource_models/transporter.py",
    "resource_models/transporter_base.py",
    "resource_models/reach_in.py",
}


def _reaches_out_of_place(module: str, source: str) -> list[str]:
    if module in _MAY_REACH_A_DRIVER_PICK_OR_PLACE:
        return []
    return [f"{module}:{n}" for n in _lines_matching(_REACHES_A_DRIVER_PICK_OR_PLACE, source)]


def test_only_the_transporter_reaches_a_drivers_pick_and_place() -> None:
    offenders = [
        found for path in _SRC.rglob("*.py")
        for found in _reaches_out_of_place(path.relative_to(_SRC).as_posix(), path.read_text(encoding="utf-8"))
    ]
    assert not offenders, "Reach a driver's pick or place through Transporter.pick/place."


def test_the_reach_in_scan_fires_on_a_call_and_not_on_a_mention() -> None:
    assert _reaches_out_of_place("operations/x.py", "await arm.driver.pick_at_coords(req)\n") == ["operations/x.py:1"]
    assert _reaches_out_of_place("runtime/x.py", "with reaching_in(name):\n    pass\n") == ["runtime/x.py:1"]
    assert _reaches_out_of_place("system/x.py", "async with mover.reach_into(site, 'pick'):\n    pass\n") == [
        "system/x.py:1",
    ]
    assert _reaches_out_of_place("gateway/operator_commands.py", "with reaching_in(name):\n    pass\n") == [
        "gateway/operator_commands.py:1",
    ]
    assert _reaches_out_of_place("resource_models/transporter_base.py", "with reaching_in(name):\n    pass\n") == []
    assert _reaches_out_of_place(
        "operations/x.py", '"""Sends pick_at_coords(...) through the runner."""\nx = "place_at_coords"\n',
    ) == []
