"""A command whose outcome is unknown files an incident, and a person's decision on it settles the record.

Whoever sent it, the labware at the device goes unobserved, and the incident asks someone to confirm where
the plate is. An operator's command is not held by it: mark complete applies the command's completed effects, abort
leaves the record as it was, flagged, and extend answers that nothing is held.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cheshire_drivers.transporter_models import PickAtCoordsRequest

from orca.gateway.controller.command_events import one_command, plain_error
from orca.gateway.controller.command_pipeline import DeviceCommandPipeline, NoAnswer
from orca.gateway.controller.exceptions import CommandOutcomeUnknownError, CommandTimeoutError
from orca.resource_models.command_effects import CommandStatement
from orca.runtime.server_events import ServerEvents
from orca.resource_models.transporter import Transporter
from orca.resource_models.device_error import CommandTimeoutAbortedError
from orca.runtime.db.engine import create_memory_engine
from orca.runtime.incident_service import IncidentService
from orca.runtime.incident_store import IncidentCategory, RecoverableTimeoutContext, SystemIncident
from orca.runtime.recoverable_timeout import recoverable_timeout_coordinator
from orca.modes.run_modes import current_execution_id
from orca.operations.error_codes import ErrorCode
from orca.operations.incident_models import MarkRecoverableTimeoutCompleteRequest
from orca.operations.incidents import MarkRecoverableTimeoutCompleteOperation
from orca.operations.protocol import OperationError
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from tests.memory_activity import memory_runtime
from orca.runtime.recoverable_timeout import RecoverableTimeoutNotHeldError
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.state.ops_store import OpsHistorySearchQuery
from orca.state.records import ObservationGapCause
from tests.gateway._operator_command import run_operator_command
from tests.gateway_parts import gateway_with
from tests.test_adhoc_commands_are_recorded import WORKING_SITE, _Bench, _teachpoint, running_arm_bench
from tests.test_hand_and_run_commands_match import _run_command
from tests.test_one_recorder_for_every_device_command import SHAKER, _shaker_runtime

pytestmark = pytest.mark.asyncio


def _arm(bench: _Bench) -> Transporter:
    arm = next(mover for mover in bench.runtime.system.movers if mover.name == "arm")
    assert isinstance(arm, Transporter)
    return arm


async def _operator_pick_with_no_answer(bench: _Bench) -> str:
    """An operator's pick at the pad whose arm never answers. Returns the incident id the error carries."""
    arm = _arm(bench)
    snapshot = MagicMock(interfaces=["ITransporter"])
    tracker = MagicMock(get_device=AsyncMock(return_value=snapshot))
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        with patch.object(
            arm.driver, "pick_at_coords", AsyncMock(side_effect=CommandTimeoutError("no answer from the arm")),
        ), pytest.raises(CommandOutcomeUnknownError) as raised:
            await run_operator_command(
                gateway_with(tracker=tracker, controller=MagicMock()), bench.runtime, "arm", "pick_at_coords",
                _teachpoint("pad"),
            )
    finally:
        current_run_mode.reset(token)
    return raised.value.incident_id


async def _head_gaps(bench: _Bench, device_name: str) -> list[str]:
    hits = await bench.runtime.system.ops_history.store.search(OpsHistorySearchQuery())
    return [
        op.operation.value for _id, record in hits for op in record.operations
        if op.device_name == device_name and op.operation.value == "observation_gap"
    ]


@pytest.mark.timeout(30)
async def test_an_operator_command_with_no_answer_files_an_incident_and_flags_the_plate() -> None:
    async with running_arm_bench() as bench:
        plate = bench.runtime.system.get_labware(bench.labware_name)
        with patch.object(plate, "note_observation_gap", AsyncMock()) as flagged:
            incident_id = await _operator_pick_with_no_answer(bench)

        flagged.assert_awaited_with(ObservationGapCause.OPERATIONS_DROPPED)
        assert await _head_gaps(bench, "arm") == [], "a pick is not a tip command, so no head is flagged"
        incidents = await bench.runtime.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT)
        assert [incident.id for incident in incidents] == [incident_id]
        assert bench.ledger_site() == "pad", "nobody knows the pick happened, so the record does not move"
        with pytest.raises(RecoverableTimeoutNotHeldError):
            bench.runtime.incidents.recoverable_timeout_extend(incident_id, 30.0, confirm=True)


@pytest.mark.timeout(30)
async def test_mark_complete_on_it_applies_what_the_command_states() -> None:
    async with running_arm_bench() as bench:
        incident_id = await _operator_pick_with_no_answer(bench)

        with patch.object(bench.runtime.incidents, "_clear_device_fault", AsyncMock()) as cleared:
            await bench.runtime.incidents.recoverable_timeout_mark_complete(
                incident_id, "operator", reason="the plate is in the jaws", confirm=True,
            )

        assert bench.ledger_site() == "arm/gripper"
        cleared.assert_awaited_once_with("arm")


@pytest.mark.timeout(30)
async def test_mark_complete_applies_nothing_once_the_plate_has_been_stated_somewhere_else() -> None:
    """The pick got no answer, and a person then said where the plate is. Mark complete must not move it again."""
    async with running_arm_bench() as bench:
        incident_id = await _operator_pick_with_no_answer(bench)
        await bench.runtime.labware.edit_location(
            bench.labware_id, WORKING_SITE, reason="it is on the deck", confirm=True,
        )

        with patch.object(bench.runtime.incidents, "_clear_device_fault", AsyncMock()):
            left_as_it_is = await bench.runtime.incidents.recoverable_timeout_mark_complete(
                incident_id, "operator", reason="settling the old incident", confirm=True,
            )

        assert left_as_it_is is True
        assert bench.ledger_site() == WORKING_SITE


@pytest.mark.timeout(30)
async def test_abort_on_it_leaves_the_record_as_it_was() -> None:
    async with running_arm_bench() as bench:
        incident_id = await _operator_pick_with_no_answer(bench)

        await bench.runtime.incidents.recoverable_timeout_abort(
            incident_id, "operator", reason="the arm never moved", confirm=True,
        )

        assert bench.ledger_site() == "pad"
        acknowledged = await bench.runtime.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT)
        assert [incident.acknowledged for incident in acknowledged] == [True]


@pytest.mark.timeout(30)
async def test_an_empty_shaker_shakes_by_hand_and_in_a_run_with_the_same_record() -> None:
    """Nothing standing at a device is the normal case: the command runs and its record names no labware."""
    runtime = await _shaker_runtime()
    try:
        snapshot = MagicMock(interfaces=["IShaker"])
        controller = MagicMock(execute_command=AsyncMock(return_value=None))
        await run_operator_command(
            gateway_with(tracker=MagicMock(get_device=AsyncMock(return_value=snapshot)), controller=controller),
            runtime, SHAKER, "shake", {"speed": 500, "duration": 1},
        )
        await _run_command(runtime, SHAKER, "shake", (1.0, 500.0))

        hits = await runtime.system.ops_history.store.search(OpsHistorySearchQuery())
        shakes = [op for _id, record in hits for op in record.operations if op.device_name == SHAKER]
        assert len(shakes) == 2
        assert [(op.operation, op.affected_labware) for op in shakes] == [(shakes[0].operation, [])] * 2
    finally:
        await runtime.shutdown()


async def _operator_pick_stopped_mid_way(bench: _Bench) -> None:
    """An operator's pick at the pad, cancelled while the arm is running it."""
    arm = _arm(bench)
    snapshot = MagicMock(interfaces=["ITransporter"])
    tracker = MagicMock(get_device=AsyncMock(return_value=snapshot))
    reached = asyncio.Event()

    async def runs_until_cancelled(request: PickAtCoordsRequest) -> None:
        reached.set()
        await asyncio.Event().wait()

    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        with patch.object(arm.driver, "pick_at_coords", runs_until_cancelled):
            sent = asyncio.create_task(run_operator_command(
                gateway_with(tracker=tracker, controller=MagicMock()), bench.runtime, "arm", "pick_at_coords",
                _teachpoint("pad"),
            ))
            await reached.wait()
            sent.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sent
    finally:
        current_run_mode.reset(token)


@pytest.mark.timeout(30)
async def test_an_operator_command_stopped_mid_way_files_an_incident_too() -> None:
    async with running_arm_bench() as bench:
        await _operator_pick_stopped_mid_way(bench)
        await bench.runtime.system.command_pipeline.stop_reports_written()

        incidents = await bench.runtime.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT)
        assert ["stopped" in incident.message for incident in incidents] == [True]
        assert bench.ledger_site() == "pad"


class _Hears:
    def __init__(self) -> None:
        self.devices: list[str] = []

    async def no_answer(
        self, device_name: str, command: str, command_id: str, execution_id: str | None,
        seconds: float, why: NoAnswer, statement: CommandStatement | None, named: Sequence[str],
    ) -> str | None:
        self.devices.append(device_name)
        return f"incident-{device_name}"


async def _no_answer() -> None:
    raise CommandTimeoutError("no answer")


async def _nothing_saved() -> None:
    return None


@pytest.mark.timeout(30)
async def test_a_step_before_the_senders_command_starts_reports_its_own_no_answer() -> None:
    """A site readied before an operator's pick (a door opening) is reported for itself, once."""
    pipeline: DeviceCommandPipeline[CommandStatement] = DeviceCommandPipeline(())
    heard = _Hears()
    pipeline.report_to(ServerEvents(), heard, plain_error)

    async with one_command("arm", "pick_at_coords", {}, error_text=plain_error):
        with pytest.raises(CommandOutcomeUnknownError, match="incident-lh"):
            await pipeline.run_engine_command("lh", "open", _no_answer, _nothing_saved, carries_out=None)

    assert heard.devices == ["lh"]


async def _stopped() -> None:
    raise asyncio.CancelledError


def _a_pick_whose_door(pipeline: DeviceCommandPipeline[CommandStatement], door: Callable[[], Awaitable[None]]) -> Awaitable[None]:
    """An arm's pick that opens the site's door first, as a move does: the door's `open` is a command of its own."""
    async def pick_through_the_door() -> None:
        await pipeline.run_engine_command("lh", "open", door, _nothing_saved, carries_out=None)

    return pipeline.run_engine_command("arm", "pick_at_coords", pick_through_the_door, _nothing_saved, carries_out=None)


@pytest.mark.timeout(30)
async def test_a_door_with_no_answer_inside_a_pick_files_one_incident_for_the_door() -> None:
    pipeline: DeviceCommandPipeline[CommandStatement] = DeviceCommandPipeline(())
    heard = _Hears()
    pipeline.report_to(ServerEvents(), heard, plain_error)

    with pytest.raises(CommandOutcomeUnknownError, match="incident-lh"):
        await _a_pick_whose_door(pipeline, _no_answer)

    assert heard.devices == ["lh"]


@pytest.mark.timeout(30)
async def test_a_door_stopped_mid_way_inside_a_pick_files_one_incident_for_the_door() -> None:
    pipeline: DeviceCommandPipeline[CommandStatement] = DeviceCommandPipeline(())
    heard = _Hears()
    pipeline.report_to(ServerEvents(), heard, plain_error)

    with pytest.raises(asyncio.CancelledError):
        await _a_pick_whose_door(pipeline, _stopped)
    await pipeline.stop_reports_written()

    assert heard.devices == ["lh"]


@pytest.mark.timeout(30)
async def test_a_run_command_aborted_after_its_timeout_has_one_incident() -> None:
    """One unknown outcome is one incident: aborting the run's held timeout settles that incident and files no other."""
    built = await _shaker_runtime()
    assert isinstance(built, SystemRuntime)
    runtime = built
    try:
        shaker = runtime.system.get_device(SHAKER)

        async def never_answers(duration: float, speed: float) -> None:
            await asyncio.Event().wait()

        declared = asyncio.Event()
        declare = runtime.declare_recoverable_timeout

        def declare_and_tell(execution_id: str, context: RecoverableTimeoutContext) -> SystemIncident:
            incident = declare(execution_id, context)
            declared.set()
            return incident

        coordinator_token = recoverable_timeout_coordinator.set(runtime.recoverable_timeout_coordinator)
        execution_token = current_execution_id.set("run_1")
        try:
            # A request's own duration extends its limit, so this shake is asked for less than the limit.
            with patch.object(shaker, "shake", never_answers), patch.object(
                shaker, "command_max_seconds", MagicMock(return_value=0.01),
            ), patch.object(
                shaker, "command_typical_seconds", MagicMock(return_value=0.0),
            ), patch.object(runtime, "declare_recoverable_timeout", declare_and_tell):
                running = asyncio.create_task(_run_command(runtime, SHAKER, "shake", (0.001, 500.0)))
                await declared.wait()
                [held] = await runtime.incidents.list()
                await runtime.incidents.recoverable_timeout_abort(
                    held.id, "operator", reason="the shaker never started", confirm=True,
                )
                with pytest.raises(CommandTimeoutAbortedError):
                    await running
        finally:
            current_execution_id.reset(execution_token)
            recoverable_timeout_coordinator.reset(coordinator_token)

        incidents = await runtime.incidents.list()
        assert [incident.id for incident in incidents] == [held.id]
        assert incidents[0].acknowledged
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_mark_complete_settles_the_record_after_a_restart() -> None:
    """What the command states is stored with its incident, so a runtime rebuilt from the same stores settles it."""
    engine = create_memory_engine()
    incidents = IncidentService(SqliteIncidentStore(engine))
    async with running_arm_bench(incident_service=incidents) as bench:
        incident_id = await _operator_pick_with_no_answer(bench)
        await incidents.flush_pending()
        system = bench.runtime.system
        await bench.runtime.shutdown()

        rebuilt = await memory_runtime(
            system, incident_service=IncidentService(SqliteIncidentStore(engine)),
        )
        await rebuilt.start()
        try:
            await rebuilt.incidents.recoverable_timeout_mark_complete(
                incident_id, "operator", reason="the plate is in the jaws", confirm=True,
            )
            assert bench.ledger_site() == "arm/gripper"
            [settled] = await rebuilt.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT)
            assert settled.acknowledged
        finally:
            await rebuilt.shutdown()


async def _operator_arm_command(bench: _Bench, command: str, position_id: str, answer: AsyncMock) -> None:
    arm = _arm(bench)
    tracker = MagicMock(get_device=AsyncMock(return_value=MagicMock(interfaces=["ITransporter"])))
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        with patch.object(arm.driver, command, answer):
            await run_operator_command(
                gateway_with(tracker=tracker, controller=MagicMock()), bench.runtime, "arm", command,
                _teachpoint(position_id),
            )
    finally:
        current_run_mode.reset(token)


@pytest.mark.timeout(30)
async def test_mark_complete_of_a_place_onto_a_pad_filled_since_answers_slot_occupied() -> None:
    """The place got no answer, and another plate was registered on its pad. Mark complete refuses by name."""
    async with running_arm_bench() as bench:
        await _operator_arm_command(bench, "pick_at_coords", "pad", AsyncMock(return_value=None))
        with pytest.raises(CommandOutcomeUnknownError) as raised:
            await _operator_arm_command(
                bench, "place_at_coords", "pad", AsyncMock(side_effect=CommandTimeoutError("no answer from the arm")),
            )
        blocking = await bench.runtime.labware.register("sample_plate", location="pad", confirm=True)
        request = MarkRecoverableTimeoutCompleteRequest(
            incident_id=raised.value.incident_id, operator_name="operator", reason="it was placed",
        )

        with patch.object(bench.runtime.incidents, "_clear_device_fault", AsyncMock()), pytest.raises(
            OperationError,
        ) as refused:
            await MarkRecoverableTimeoutCompleteOperation(bench.runtime).run(request)

        assert refused.value.code is ErrorCode.SLOT_OCCUPIED
        assert refused.value.extras is not None
        assert (refused.value.extras["position_id"], refused.value.extras["existing_template_name"]) == (
            "pad", "sample_plate",
        )
        blocking_name = next(lw.name for lw in await bench.runtime.labware.list_all() if lw.id == blocking.id)
        assert refused.value.extras["existing_labware_name"] == blocking_name
        assert bench.ledger_site() == "arm/gripper", "the record is left as it was"
