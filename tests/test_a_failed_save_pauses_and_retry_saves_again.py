"""A device command whose save to the record fails is saved again, and pauses at COMMAND_SAVE only if every try fails.

The device already ran the command. Neither the automatic tries nor RETRY send anything to the device.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import Mock

import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C, Teachpoint
from cheshire_drivers.shaker_models import ShakeRequest
from cheshire_drivers.sims import SimShakerDriver
from orca.devices.device_interfaces import IShaker
from orca.devices.devices import Storage
from orca.devices.shaker import Shaker
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.transporter import Transporter
from orca.runtime.execution_record import ExecutionState
from orca.runtime.incident_declarer import IncidentDeclarer
from orca.runtime.incident_service import IIncidentService
from orca.runtime.incident_store import IncidentCategory, RecoveryAction
from orca.runtime.recoverable_timeout import RecoverableTimeoutCoordinator
from orca.system.reservation_manager.errors import ActionContinuedContext, ActionFailedContext
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import DISPENSE
from orca.state.records import DeviceOperation, TrackingRecord
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from tests.ephemeral_ops_store import EphemeralOpsHistoryStore
from tests.memory_activity import memory_runtime
from tests.real_device_path import the_real_device_path
from tests.setup_package import package_stores, with_teachpoints, write_teachpoints
from tests.test_helpers import wait_for_paused_thread

pytestmark = pytest.mark.timeout(120)

WORKFLOW = "shake_once"
SAVE_TRIES = 4


def _is_the_shake(record: TrackingRecord) -> bool:
    return any(op.operation is DeviceOperation.SHAKE for op in record.operations)


class _RefusesTheShake(EphemeralOpsHistoryStore):
    """An operation record that cannot write the shake the first `times` it is asked."""

    def __init__(self, times: int) -> None:
        super().__init__()
        self.times = times
        self.refused = 0

    async def append(self, execution_id: str, record: TrackingRecord) -> None:
        if _is_the_shake(record) and self.refused < self.times:
            self.refused += 1
            raise OSError("disk full")
        await super().append(execution_id, record)


class _CountsShakes(SimShakerDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shakes: list[ShakeRequest] = []

    async def shake(self, request: ShakeRequest) -> None:
        self.shakes.append(request)
        await super().shake(request)


@dataclass
class _Bench:
    runtime: SystemRuntime
    store: _RefusesTheShake
    shaker: _CountsShakes

    async def paused_at_the_save(self) -> tuple[str, ThreadSnapshot]:
        execution_id = (await self.runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)).id
        return execution_id, await wait_for_paused_thread(self.runtime, execution_id, timeout=60.0)

    async def shakes_saved(self, execution_id: str) -> int:
        return sum(1 for record in await self.store.list(execution_id) if _is_the_shake(record))


@asynccontextmanager
async def _bench(refusals: int = SAVE_TRIES) -> AsyncIterator[_Bench]:
    path = the_real_device_path()
    stores = package_stores()
    shaker_driver = _CountsShakes("shaker")
    path.simulate_with("shaker", shaker_driver)
    plate = PlateTemplate("plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    shaker = Shaker("shaker")
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("shaker", C(200, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
    ]))

    @orca.action(device=shaker, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=500)

    @orca.method(failure_policy=FailurePolicy.PAUSE)
    async def shake_method(ctx: MethodContext):
        yield shake

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield shake_method

    @orca.workflow(name=WORKFLOW)
    def workflow(wf):
        wf.start(plate_journey)

    store = _RefusesTheShake(refusals)
    build = await orca.build_system(
        name="Save bench", workflow=workflow,
        topology=Topology(
            locations={"stacker": Storage("stacker"), "shaker": shaker, "waste": Storage("waste")},
            transporters=[arm],
        ),
        stores=stores, event_bus=EventBus(), ops_history_store=store,
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        yield _Bench(runtime, store, shaker_driver)
    finally:
        await runtime.shutdown()


async def test_a_save_that_fails_twice_is_saved_on_its_third_try_and_the_run_never_pauses() -> None:
    async with _bench(refusals=2) as bench:
        execution_id = (await bench.runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)).id

        ended = await asyncio.wait_for(bench.runtime.wait(execution_id), timeout=60.0)
        assert ended.status is ExecutionState.COMPLETED
        assert bench.store.refused == 2
        assert len(bench.shaker.shakes) == 1
        assert await bench.shakes_saved(execution_id) == 1
        assert await bench.runtime.incidents.list(
            execution_id=execution_id, category=IncidentCategory.ACTION_FAILED,
        ) == []


async def test_a_save_that_fails_every_try_pauses_at_its_save_after_one_send() -> None:
    async with _bench() as bench:
        execution_id, thread = await bench.paused_at_the_save()

        assert thread.pause_site == "COMMAND_SAVE"
        assert bench.store.refused == SAVE_TRIES
        assert len(bench.shaker.shakes) == 1
        assert await bench.shakes_saved(execution_id) == 0


async def test_a_command_whose_save_failed_pauses_at_its_save_and_offers_retry_not_retry_action() -> None:
    async with _bench() as bench:
        _execution_id, thread = await bench.paused_at_the_save()

        assert thread.pause_site == "COMMAND_SAVE"
        assert thread.paused_device_command == "shake"
        assert list(thread.honoured_decisions) == ["RETRY", "CONTINUE", "ABORT_ACTION", "ABORT_METHOD", "ABORT_THREAD"]


async def test_retry_saves_the_answer_again_without_sending_the_command_and_the_run_completes() -> None:
    async with _bench() as bench:
        execution_id, thread = await bench.paused_at_the_save()
        assert await bench.shakes_saved(execution_id) == 0

        await bench.runtime.threads.recover(execution_id, thread.id, RecoveryDecision.RETRY, confirm=True)

        ended = await asyncio.wait_for(bench.runtime.wait(execution_id), timeout=60.0)
        assert ended.status is ExecutionState.COMPLETED
        assert len(bench.shaker.shakes) == 1
        assert await bench.shakes_saved(execution_id) == 1


async def test_continue_carries_on_with_the_result_unsaved_and_names_it_on_an_incident() -> None:
    async with _bench() as bench:
        execution_id, thread = await bench.paused_at_the_save()

        await bench.runtime.threads.recover(execution_id, thread.id, RecoveryDecision.CONTINUE, confirm=True)

        ended = await asyncio.wait_for(bench.runtime.wait(execution_id), timeout=60.0)
        assert ended.status is ExecutionState.COMPLETED
        assert len(bench.shaker.shakes) == 1
        assert await bench.shakes_saved(execution_id) == 0
        [continued] = await bench.runtime.incidents.list(
            execution_id=execution_id, category=IncidentCategory.ACTION_CONTINUED,
        )
        assert isinstance(continued.detail, ActionContinuedContext)
        assert continued.detail.unsaved_command == "shake"
        assert json.loads(continued.detail.unsaved_result or "") is None
        assert json.loads(continued.detail.unsaved_request or "")["kwargs"] == {"duration": 1, "speed": 500}
        assert "'shake' is not on the record" in continued.message


async def test_retry_action_is_refused_and_the_thread_stays_paused_at_the_save() -> None:
    async with _bench() as bench:
        execution_id, thread = await bench.paused_at_the_save()

        with pytest.raises(ValueError, match="COMMAND_SAVE and does not honour RETRY_ACTION"):
            await bench.runtime.threads.recover(execution_id, thread.id, RecoveryDecision.RETRY_ACTION, confirm=True)

        [still] = [t for t in bench.runtime.list_threads(execution_id) if t.id == thread.id]
        assert still.pause_site == "COMMAND_SAVE"
        assert len(bench.shaker.shakes) == 1


async def test_the_incident_for_a_failed_save_recommends_retry() -> None:
    async with _bench() as bench:
        execution_id, _thread = await bench.paused_at_the_save()

        [incident] = await bench.runtime.incidents.list(
            execution_id=execution_id, category=IncidentCategory.ACTION_FAILED,
        )
        assert incident.recovery_action is RecoveryAction.THREAD_RECOVER_RETRY
        assert "CommandNotSavedError" in incident.message


def _recommended_for(device_command: str | None) -> RecoveryAction:
    service = Mock(spec=IIncidentService)
    declarer = IncidentDeclarer(service, Mock(), Mock(spec=RecoverableTimeoutCoordinator), lambda _id: None)
    declarer.declare_action_failure("e1", "t1", ActionFailedContext(
        action_command="shake", method_name="shake_method", error_type="CommandNotSavedError",
        error_message="saving it failed", device_command=device_command,
    ))
    recommended: RecoveryAction = service.record.call_args.kwargs["recovery_action"]
    return recommended


def test_an_action_failure_stopped_at_one_command_recommends_retry() -> None:
    """A failed device call or a failed save of one: both name the command, and RETRY repeats only that step."""
    assert _recommended_for("shake") is RecoveryAction.THREAD_RECOVER_RETRY


def test_an_action_whose_own_code_failed_recommends_retry_action() -> None:
    assert _recommended_for(None) is RecoveryAction.THREAD_RECOVER_RETRY_ACTION
