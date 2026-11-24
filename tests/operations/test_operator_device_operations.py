"""The operator device Operations: served by every server from one catalog, and each one's own logic."""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import ClassVar

from typing_extensions import Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.command_table import DeviceCommand
from cheshire_drivers.command_responses import EmptyCommandResponse
from pydantic import BaseModel, JsonValue

from orca.api import GenericOperationCatalog, IApiContext, OperationEntry
from orca.api.mount import refuse_two_routes_at_one_path
from orca.daemon.operation_catalog import DaemonOperationCatalog
from orca.gateway.controller.exceptions import DeviceLockedError, DeviceOfflineError
from orca.gateway.device_command_runner import NamedCommandResult
from orca.gateway.registry import DeviceSnapshot
from orca.operations.binding import RestBinding
from cheshire_drivers.driver_introspection import MethodInfo
from orca.operations.operator_device import (
    reads_a_property,
    OWN_LOGIC_OPERATIONS,
    DeviceBatchExecuteOperation,
    LiquidHandlerConfigureDeckOperation,
    SaveCurrentPositionOperation,
    TransporterPickPlateOperation,
    TransporterPlacePlateOperation,
    claims_name_commands,
    require_kind,
)
from orca.operations.operator_device_models import (
    BatchExecuteRequest,
    DeviceKind,
    ConfigureDeckRequest,
    PickPlateRequest,
    PlacePlateRequest,
)
from orca.operations.protocol import OperationError
from orca.modes.run_modes import WorkflowRunMode


def _snapshot(interfaces: list[str]) -> DeviceSnapshot:
    return DeviceSnapshot(
        type="device", name="dev", interfaces=interfaces, capabilities=[], provides_state=False, methods={},
        site="s", lab="l", workcell=None, status="ready", last_seen=datetime.now(timezone.utc),
    )


class _Runner:
    """A runner that keeps the order of what it was asked to do."""

    def __init__(self, interfaces: list[str], fail_named: Exception | list[Exception] | None = None) -> None:
        self.calls: list[str] = []
        self.sent: list[dict[str, JsonValue]] = []
        self.reasons: list[str | None] = []
        self._interfaces = interfaces
        self._fail_named = fail_named

    async def device(self, device_id: str) -> DeviceSnapshot:
        return _snapshot(self._interfaces)

    async def run(
        self, device_id: str, command: DeviceCommand, params: BaseModel, *, reason: str | None = None,
    ) -> BaseModel:
        self.calls.append(f"run {command.name}")
        self.sent.append(params.model_dump(mode="json", exclude_unset=True))
        return EmptyCommandResponse()

    async def run_named(
        self, device_id: str, command: str, params: Mapping[str, JsonValue],
        *, timeout_seconds: float | None = None, confirm: bool = False, reason: str | None = None,
    ) -> NamedCommandResult:
        self.calls.append(f"named {command}")
        self.sent.append(dict(params))
        self.reasons.append(reason)
        if isinstance(self._fail_named, list):
            if self._fail_named:
                raise self._fail_named.pop(0)
        elif self._fail_named is not None:
            raise self._fail_named
        return NamedCommandResult(duration_ms=1.0)

    @asynccontextmanager
    async def hold(self, device_id: str) -> AsyncIterator[Self]:
        self.calls.append("hold")
        yield self
        self.calls.append("release")


class _Devices:
    """The device facade's answer a command by name needs: what name goes out."""

    def command_name(self, device_name: str, name: str) -> str:
        return name.removeprefix("shaker.")


def _named_runtime(devices: _Devices | None = None) -> MagicMock:
    """A runtime holding only what a command by name reads off it."""
    runtime = MagicMock()
    runtime.devices = devices or _Devices()
    runtime.operator_base_mode = MagicMock(return_value=WorkflowRunMode.LIVE)
    return runtime


def _batch(runtime: MagicMock, runner: _Runner) -> DeviceBatchExecuteOperation:
    return DeviceBatchExecuteOperation(runtime=runtime, devices=runner)


def _served(entries: Sequence[OperationEntry[IApiContext]]) -> set[type]:
    return {entry.op_class for entry in entries}


@pytest.mark.parametrize("op_class", [*OWN_LOGIC_OPERATIONS, DeviceBatchExecuteOperation, SaveCurrentPositionOperation])
def test_every_operator_device_command_is_served_by_the_framework_from_the_shared_catalog(op_class: type) -> None:
    assert op_class in _served(GenericOperationCatalog().entries())
    assert op_class in {entry.op_class for entry in DaemonOperationCatalog().entries()}


def test_the_own_logic_claims_each_name_a_command() -> None:
    claims_name_commands(OWN_LOGIC_OPERATIONS)


class _ClaimsNothingReal:
    claims: ClassVar[tuple[tuple[str, str], ...]] = (("liquid_handler", "no_such_command"),)


class _ClaimsAspirateToo:
    claims: ClassVar[tuple[tuple[str, str], ...]] = (("liquid_handler", "aspirate"),)


def test_a_claim_that_names_no_command_is_refused() -> None:
    with pytest.raises(ValueError, match="names no command"):
        claims_name_commands((_ClaimsNothingReal,))


def test_two_operations_claiming_one_command_are_refused() -> None:
    with pytest.raises(ValueError, match="both claim"):
        claims_name_commands((*OWN_LOGIC_OPERATIONS, _ClaimsAspirateToo))


class _AlsoAtPickPlate:
    Request: ClassVar[type[BaseModel]] = PickPlateRequest
    Response: ClassVar[type[BaseModel]] = EmptyCommandResponse
    binding: ClassVar[RestBinding] = TransporterPickPlateOperation.binding

    async def run(self, req: BaseModel) -> BaseModel:
        return EmptyCommandResponse()


def test_two_routes_at_one_path_are_refused() -> None:
    entries: list[OperationEntry[IApiContext]] = [
        OperationEntry(TransporterPickPlateOperation, lambda _c: _AlsoAtPickPlate()),
        OperationEntry(_AlsoAtPickPlate, lambda _c: _AlsoAtPickPlate()),
    ]
    with pytest.raises(ValueError, match="can both be reached by one url"):
        refuse_two_routes_at_one_path(entries)
    refuse_two_routes_at_one_path(entries[:1])


def _at(method: str, path: str) -> type:
    class _Route:
        Request: ClassVar[type[BaseModel]] = _Path
        Response: ClassVar[type[BaseModel]] = EmptyCommandResponse
        binding: ClassVar[RestBinding] = RestBinding(method="DELETE" if method == "DELETE" else "GET", path=path, tag="t")

        async def run(self, req: BaseModel) -> BaseModel:
            return EmptyCommandResponse()

    _Route.__name__ = f"{method} {path}"
    return _Route


class _Path(BaseModel):
    a: str = ""
    b: str = ""
    c: str = ""


@pytest.mark.parametrize(("first", "second"), [
    ("/x/{a}/{b:path}", "/x/{a}/{b:path}/y/{c}"),
    ("/x/by-c/{a}", "/x/{a}/y"),
    ("/x/{a}", "/x/y"),
])
def test_two_templates_one_url_could_reach_are_refused_whatever_their_order(first: str, second: str) -> None:
    """A url that matches two templates would go to whichever was mounted first."""
    one, other = _at("GET", first), _at("GET", second)
    for entries in ([one, other], [other, one]):
        with pytest.raises(ValueError, match="can both be reached by one url"):
            refuse_two_routes_at_one_path([OperationEntry(op, lambda _c: _AlsoAtPickPlate()) for op in entries])


@pytest.mark.parametrize(("first", "second"), [
    ("/x/{a}/{b:path}", "/z/{a}/{c}/{b:path}"),
    ("/x/{a}", "/x/{a}/y"),
    ("/x/{a}/y", "/x/{a}/z"),
])
def test_templates_no_url_could_reach_together_are_mounted(first: str, second: str) -> None:
    refuse_two_routes_at_one_path([
        OperationEntry(op, lambda _c: _AlsoAtPickPlate()) for op in (_at("GET", first), _at("GET", second))
    ])


def test_the_same_template_under_two_methods_is_two_routes() -> None:
    refuse_two_routes_at_one_path([
        OperationEntry(_at("GET", "/x/{a}"), lambda _c: _AlsoAtPickPlate()),
        OperationEntry(_at("DELETE", "/x/{a}"), lambda _c: _AlsoAtPickPlate()),
    ])


@pytest.mark.parametrize(("op_class", "command"), [
    (TransporterPickPlateOperation, "pick_at_coords"),
    (TransporterPlacePlateOperation, "place_at_coords"),
])
async def test_a_pick_or_place_plate_holds_the_arm_across_its_move_and_names_only_the_site(
    op_class: type[TransporterPickPlateOperation] | type[TransporterPlacePlateOperation], command: str,
) -> None:
    """The arm resolves its own taught position, waypoints and grip, as in a run, so the Operation sends only the site."""
    runner = _Runner(["ITransporter"])

    fields = {"device_id": "arm", "position_id": "hotel_1"}
    if op_class is TransporterPickPlateOperation:
        await TransporterPickPlateOperation(devices=runner).run(PickPlateRequest.model_validate(fields))
    else:
        await TransporterPlacePlateOperation(devices=runner).run(PlacePlateRequest.model_validate(fields))

    assert runner.calls == ["hold", f"named {command}", "release"]
    assert runner.sent == [{"teachpoint": {"position_id": "hotel_1"}}]


async def test_a_batch_runs_under_one_hold_and_a_failure_answers_batch_error_with_what_ran() -> None:
    runner = _Runner(["IShaker"], fail_named=DeviceOfflineError("gone"))

    with pytest.raises(OperationError) as failed:
        await _batch(_named_runtime(), runner).run(BatchExecuteRequest.model_validate(
            {"device_id": "shaker", "commands": [{"command": "open"}, {"command": "close"}]},
        ))

    assert runner.calls == ["hold", "named open", "release"]
    assert failed.value.surface_code() == "batch_error"
    assert failed.value.extras is not None and failed.value.extras["failed_at_index"] == 0


async def test_a_batch_command_with_a_string_confirm_is_refused_before_anything_runs() -> None:
    runner = _Runner(["IShaker"])

    with pytest.raises(OperationError) as refused:
        await _batch(_named_runtime(), runner).run(BatchExecuteRequest.model_validate(
            {"device_id": "shaker", "commands": [{"command": "open", "confirm": "false"}]},
        ))

    assert refused.value.surface_code() == "validation_error"
    assert runner.calls == []


def _one(command: str, **fields: JsonValue) -> BatchExecuteRequest:
    return BatchExecuteRequest.model_validate({"device_id": "shaker", "commands": [{"command": command}], **fields})


async def test_one_command_by_name_is_a_batch_of_one_under_its_own_hold() -> None:
    runner = _Runner(["IShaker"])

    answer = await _batch(_named_runtime(), runner).run(_one("shaker.shake"))

    assert runner.calls == ["hold", "named shake", "release"]
    assert [r.command for r in answer.results] == ["shaker.shake"]


async def test_a_command_by_name_on_a_known_device_with_no_runtime_is_refused_as_not_ready() -> None:
    runner = _Runner(["IShaker"])

    with pytest.raises(OperationError) as refused:
        await DeviceBatchExecuteOperation(runtime=None, devices=runner).run(_one("shake"))

    assert refused.value.surface_code() == "runtime_not_ready"
    assert runner.calls == []


@pytest.mark.parametrize(("interfaces", "command", "route"), [
    (["ITransporter"], "pick_at_coords", "POST /api/transporters/{device_id}/pick-plate"),
    (["ITransporter"], "place_at_coords", "POST /api/transporters/{device_id}/place-plate"),
    (["ITransporter"], "open_gripper", "POST /api/transporters/{device_id}/open-gripper"),
    (["ILiquidHandler"], "configure_deck", "POST /api/liquid-handlers/{device_id}/configure-deck"),
    (["ILiquidHandler"], "aspirate", "POST /api/liquid-handlers/{device_id}/aspirate"),
])
async def test_a_command_an_operation_claims_is_refused_by_name_before_anything_runs(
    interfaces: list[str], command: str, route: str,
) -> None:
    """Sent by name it would skip the Operation's own checks, so the answer names the route that runs them."""
    runner = _Runner(interfaces)

    with pytest.raises(OperationError) as refused:
        await _batch(_named_runtime(), runner).run(BatchExecuteRequest.model_validate(
            {"device_id": "shaker", "commands": [{"command": "home"}, {"command": command}]},
        ))

    assert refused.value.surface_code() == "validation_error"
    assert refused.value.extras is not None
    assert refused.value.extras["send_with"] == {"rest": route, "cli": None}
    assert runner.calls == []


async def test_a_claimed_name_on_a_device_without_that_interface_is_sent() -> None:
    """A claim is about one interface: another device's command of the same name has no Operation of its own."""
    runner = _Runner(["IShaker"])

    await _batch(_named_runtime(), runner).run(_one("open_gripper"))

    assert runner.calls == ["hold", "named open_gripper", "release"]


async def test_each_command_of_a_batch_hands_the_runner_its_reason() -> None:
    runner = _Runner(["IShaker"])

    await _batch(_named_runtime(), runner).run(BatchExecuteRequest.model_validate(
        {"device_id": "shaker", "commands": [{"command": "open"}, {"command": "close"}], "reason": "lid check"},
    ))

    assert runner.reasons == ["lid check", "lid check"]


@pytest.mark.parametrize("command", ["is_initialized", "is_connected"])
async def test_a_driver_property_sent_by_name_is_refused_and_names_the_registry(command: str) -> None:
    runner = _Runner(["IShaker"])

    with pytest.raises(OperationError) as refused:
        await _batch(_named_runtime(), runner).run(_one(command))

    assert refused.value.surface_code() == "validation_error"
    assert "GET /api/devices/shaker." in str(refused.value)
    assert not any(call.startswith("named") for call in runner.calls)


def test_a_vendor_property_the_device_advertises_is_a_reading() -> None:
    advertised = _snapshot(["IShaker"]).model_copy(update={"methods": {
        "lamp_on": MethodInfo(kind="property", params={}), "home": MethodInfo(kind="method", params={}),
    }})

    assert reads_a_property(advertised, "lamp_on")
    assert not reads_a_property(advertised, "home")
    assert not reads_a_property(advertised, "shake")


async def test_a_command_by_name_on_a_device_the_engine_holds_is_refused_at_once() -> None:
    """An operator is refused while the engine holds the device, as on every other operator route."""
    runner = _Runner(["IShaker"], fail_named=[DeviceLockedError("busy")])

    with pytest.raises(OperationError) as refused:
        await _batch(_named_runtime(), runner).run(_one("open"))

    assert refused.value.surface_code() == "batch_error"
    assert refused.value.extras is not None
    assert refused.value.extras["results"] == [{
        "command": "open", "success": False, "duration_ms": 0.0, "result": None,
        "error": "busy", "error_type": "DeviceLockedError",
    }]
    assert runner.calls == ["hold", "named open", "release"]


async def test_a_command_by_name_passes_its_timeout_to_the_runner() -> None:
    sent: list[float | None] = []

    class _Timed(_Runner):
        async def run_named(
            self, device_id: str, command: str, params: Mapping[str, JsonValue],
            *, timeout_seconds: float | None = None, confirm: bool = False, reason: str | None = None,
        ) -> NamedCommandResult:
            sent.append(timeout_seconds)
            return NamedCommandResult(duration_ms=1.0)

    await _batch(_named_runtime(), _Timed(["IShaker"])).run(BatchExecuteRequest.model_validate(
        {"device_id": "shaker", "commands": [{"command": "open", "timeout": 12.5}]},
    ))

    assert sent == [12.5]


async def test_a_device_the_caller_may_not_see_answers_auth_error() -> None:
    """The connection roster raises RuntimeError for a device outside the caller's reach."""

    class _Hidden(_Runner):
        async def device(self, device_id: str) -> DeviceSnapshot:
            raise RuntimeError("not yours")

    with pytest.raises(OperationError) as refused:
        await require_kind(_Hidden(["IShaker"]), "shaker", DeviceKind.DEVICE)

    assert (refused.value.surface_code(), refused.value.http_status) == ("auth_error", 401)
