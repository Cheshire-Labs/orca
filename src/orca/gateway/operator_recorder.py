"""What an operator's device command leaves behind in the record, saved through the one `CommandRecorder`.

What each command did is read by `orca.state.command_operations`, the reader a workflow action's call goes
through, and saved through `CommandRecorder.save_operations`, the save point a workflow action's call uses.
Only the identity differs: an operator's records carry TrackingSource.OPERATOR and file under the system
bucket, because no execution asked for them.

Where a command moves labware is stated as data (`operator_statement`) and written by the pipeline's effects, as
for a run. A tip pick-up passes the same check before it is sent as a run's (`check_before_sending`).
"""

import logging
from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from cheshire_drivers.command_table import COMMANDS
from cheshire_drivers.liquid_handler_models import (
    LabwareStateResponse,
    MovePlateRequest,
)
from pydantic import BaseModel, JsonValue, ValidationError

from orca.gateway.controller.command_events import params_as_sent
from orca.gateway.controller.command_pipeline import PartialChannelFailureError
from orca.gateway.controller.exceptions import InvalidCommandError
from orca.resource_models.command_effects import CommandStatement, device_move
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.resources import IResource
from orca.resource_models.tip_preflight import refuse_a_pick_from_empty_spots, tips_claimed_by
from orca.state.command_operations import (
    args_text,
    labware_named_in,
    operations_for,
    operations_for_completed,
    outside_a_run,
    save_outside_a_run,
)
from orca.state.mounted import name_the_rack_a_96_return_went_to
from orca.state.records import OperationRecord, TrackingSource
from orca.devices.interpreter_resolution import resolve_operation_interpreter
from orca.runtime.runtime_interface import ISystemRuntime
from orca.workflow_models.actions.device_call_dispatcher import CommandRecorder
from orca.system.system_interface import ISystem

logger = logging.getLogger("orca")


@runtime_checkable
class _ReportsDriverState(Protocol):
    """A device whose driver reports labware state, and which knows how much of
    that report the deployment has agreed to believe."""

    def filter_driver_state(
        self, response: LabwareStateResponse,
    ) -> LabwareStateResponse: ...

_LIQUID_HANDLER = "ILiquidHandler"


async def record_operator_command(
    system: ISystem,
    device_name: str,
    command: str,
    params: Mapping[str, JsonValue],
    result: JsonValue = None,
    *,
    interfaces: frozenset[str],
) -> None:
    """Write what an ad-hoc command did to the record.

    Every command is recorded as an operation. ``interfaces`` are the device's: a command is read as a liquid
    handler's only on a liquid handler, because other devices reuse its names. A failed channel is saved per
    channel and then fails the command, as a run's does.
    """
    is_liquid_handler = _LIQUID_HANDLER in interfaces
    request = _parse_request(command, params) if is_liquid_handler else None
    report = result if is_liquid_handler else None
    await _record_operations(system, device_name, command, params, request, report)


def operator_statement(
    system: ISystem, device_name: str, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
) -> CommandStatement | None:
    """What an operator's command states it does to labware, as data. Only a liquid handler's plate move moves one."""
    if _LIQUID_HANDLER not in interfaces:
        return None
    request = _parse_request(command, params)
    if not isinstance(request, MovePlateRequest) or request.plate is None:
        return None
    instance = _labware_named(system, request.plate)
    target = _deck_location(system, device_name, request.to_position)
    if instance is None or target is None:
        # A plate the driver knows and the record does not, or a site the layout does not provide: nothing to move.
        return None
    return device_move(device_name, instance, _present_location(system, instance), target)


def labware_named_by(
    device_name: str, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
) -> list[str]:
    """The labware an operator's command names as its own: empty for a shake, a discard or any that names none."""
    if _LIQUID_HANDLER not in interfaces:
        return []
    context = outside_a_run(device_name, command, TrackingSource.OPERATOR)
    return labware_named_in(operations_for(command, _parse_request(command, params), args_text(params), context))


async def check_before_sending(
    system: ISystem, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
) -> None:
    """The checks a run's command passes before it is sent: today, that the record has the tips a pick-up takes."""
    if _LIQUID_HANDLER not in interfaces:
        return
    request = _parse_request(command, params)
    rack_named: Callable[[str], LabwareInstance | None] = lambda name: _labware_named(system, name)
    await refuse_a_pick_from_empty_spots(tips_claimed_by(request, rack_named), rack_named)


def operator_params_as_sent(
    command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
) -> dict[str, JsonValue]:
    """A liquid-handler command's params as the wire carries them, as a run's event names them; others as given.

    Params that do not fit stay as given: the check before sending refuses them as invalid.
    """
    if _LIQUID_HANDLER in interfaces:
        try:
            request = _parse_request(command, params)
        except InvalidCommandError:
            request = None
        if request is not None:
            return params_as_sent(request)
    return dict(params)


def _parse_request(
    command: str, params: Mapping[str, JsonValue],
) -> BaseModel | None:
    """The typed wire request for a liquid-handler command, or None for a vendor extra.

    Read before the command is sent, so params that do not fit are refused here as the controller would refuse them.
    """
    found = COMMANDS.find({_LIQUID_HANDLER}, command)
    if found is None:
        return None
    try:
        return found.validate(params)
    except ValidationError as exc:
        raise InvalidCommandError(f"{command!r}: the params do not fit its request: {exc}") from exc


def _deck_location(
    system: ISystem, device_name: str, site: str,
) -> Location | None:
    """The engine Location behind a driver deck site.

    Drivers name sites bare (``carrier-7-0``, ``B2-slot``); the engine addresses
    them under the device's mutex (``lh/carrier-7-0``). A site the layout does
    not provide, or a device the topology does not declare, resolves to nothing
    rather than a guess.
    """
    system_map = system.system_map
    try:
        device_location = system_map.get_resource_location(device_name)
    except ValueError:
        return None
    prefix = device_location.owner_mutex_id or device_location.position_id
    try:
        return system_map.get_location(f"{prefix}/{site}")
    except KeyError:
        return None


# -- Operations ---------------------------------------------------------------


async def _record_operations(
    system: ISystem,
    device_name: str,
    command: str,
    params: Mapping[str, JsonValue],
    request: BaseModel | None,
    result: JsonValue,
) -> None:
    """Read the command through the same reader a workflow action uses.

    OPERATOR, not OBSERVED: the fold reads the operation and ignores the
    source, so this is purely how an auditor tells a hand-sent command from
    the engine's own.
    """
    context = outside_a_run(
        device_name, command, TrackingSource.OPERATOR,
        resolve_labware_id=lambda name: _id_of(system, name),
        default_labware=[labware.name for labware in system.labware_at_device(device_name)],
    )

    async def save(records: list[OperationRecord]) -> None:
        records = await name_the_rack_a_96_return_went_to(
            records, system.ops_history, lambda name: _id_of(system, name),
        )
        await save_outside_a_run(system.ops_history, context, records)

    recorder = CommandRecorder(
        resolve_operation_interpreter(_resource(system, device_name)), save,
        device_name, context.action_id, context.thread_id,
    )
    operations, channel_failed = operations_for_completed(
        command, request, args_text(params), _driver_answer(system, device_name, command, result), context,
    )
    await recorder.save_operations(operations, f"the operator's {command}")
    if channel_failed:
        raise PartialChannelFailureError(operations)


def _driver_answer(
    system: ISystem, device_name: str, command: str, result: JsonValue,
) -> LabwareStateResponse | None:
    """The driver's answer, held to the same trust setting a workflow action's is.

    An answer that does not fit its model is left out with a warning: the
    command's own operations are still saved.
    """
    found = COMMANDS.find({_LIQUID_HANDLER}, command)
    if found is None or not isinstance(result, dict):
        return None
    device = _resource(system, device_name)
    if not isinstance(device, _ReportsDriverState):
        return None
    try:
        response = found.response_model.model_validate(result)
    except ValidationError as exc:
        logger.warning("%s's answer to %r does not fit its model; it is not on the record: %s", device_name, command, exc)
        return None
    if not isinstance(response, LabwareStateResponse):
        return None
    return device.filter_driver_state(response)


def _labware_named(system: ISystem, name: str) -> LabwareInstance | None:
    try:
        return system.get_labware(name)
    except KeyError:
        return None


def _present_location(system: ISystem, instance: LabwareInstance) -> Location | None:
    try:
        return system.labware_location_service.get(instance)
    except KeyError:
        return None


def _id_of(system: ISystem, labware_name: str) -> str | None:
    instance = _labware_named(system, labware_name)
    return None if instance is None else instance.id


def _resource(system: ISystem, device_name: str) -> IResource | None:
    try:
        return system.get_resource(device_name)
    except KeyError:
        return None




class IOperatorCommandRecorder(Protocol):
    """What an operator's command states, checks before it is sent, and saves once it completes."""

    def statement_of(
        self, device_id: str, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
    ) -> CommandStatement | None: ...

    def labware_named_by(
        self, device_id: str, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
    ) -> list[str]: ...

    async def check_before_sending(
        self, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
    ) -> None: ...

    async def save_completed(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, JsonValue],
        result: JsonValue,
        interfaces: frozenset[str],
    ) -> None: ...


class RuntimeOperatorCommandRecorder:
    """Reads and saves an operator's command against the runtime's record."""

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    def statement_of(
        self, device_id: str, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
    ) -> CommandStatement | None:
        return operator_statement(self._runtime.system, device_id, command, params, interfaces)

    def labware_named_by(
        self, device_id: str, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
    ) -> list[str]:
        return labware_named_by(device_id, command, params, interfaces)

    async def check_before_sending(
        self, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
    ) -> None:
        await check_before_sending(self._runtime.system, command, params, interfaces)

    async def save_completed(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, JsonValue],
        result: JsonValue,
        interfaces: frozenset[str],
    ) -> None:
        # In the world an operator is in, which every device resolves without a scope.
        await record_operator_command(
            self._runtime.system, device_id, command, params,
            result=result, interfaces=interfaces,
        )
        # An operator command can move labware a waiting thread planned around.
        self._runtime.labware.tell_threads_to_look_again()
