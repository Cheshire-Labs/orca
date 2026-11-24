"""Each device keeps one driver world per run mode. A world that may not match the record is marked, and the next
command to that device rebuilds it first, whoever sends the command."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest
from cheshire_drivers.gateway_protocol import DeviceConnectInfo
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest

from orca.devices.deck_gripper_transporter import DeckGripperTransporter
from orca.devices.devices import LiquidHandler
from orca.gateway.controller.command_pipeline import DeviceCommandPipeline
from orca.gateway.controller.exceptions import CommandNotSavedError
from orca.modes.run_modes import WorkflowRunMode, mode_scope
from orca.resource_models.resource_registry import ResourceRegistry
from orca.resource_models.transporter import Transporter
from orca.system.device_worlds import DeviceWorlds, WorldDevice
from orca.system.errors import DevicesNotReadyError
from orca.modes.device_modes import DeviceMode
from tests.device_modes import set_to
from tests.memory_activity import memory_runtime
from tests.recording_handler import RecordingHandler
from tests.test_a_discharge_clears_every_deck_world import RESERVOIR_SITE, _build
from tests.test_helpers import named_for_template, run_to_quiescence

FREE_SITE = "lh/carrier-25-3"


def _registry(*resources: LiquidHandler | Transporter | DeckGripperTransporter) -> ResourceRegistry:
    registry = ResourceRegistry()
    for resource in resources:
        registry.add_resource(resource)
    return registry


def _worlds_over(
    registry: ResourceRegistry, rebuild: Callable[[WorldDevice], Awaitable[bool]],
) -> DeviceWorlds:
    worlds = DeviceWorlds(registry, rebuild)
    return worlds


# -- DeviceWorlds ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rebuild_finds_its_own_world_no_longer_owed_so_its_commands_do_not_rebuild_again() -> None:
    lh = LiquidHandler("lh")
    seen: list[bool] = []

    async def rebuild(device: WorldDevice) -> bool:
        seen.append(await worlds.owes_a_rebuild(device))
        return True

    worlds = _worlds_over(_registry(lh), rebuild)

    await worlds.bring_up_to_date("lh")

    assert seen == [False]
    assert not await worlds.owes_a_rebuild(lh)


@pytest.mark.asyncio
async def test_a_mark_set_while_the_rebuild_runs_is_kept() -> None:
    lh = LiquidHandler("lh")

    async def rebuild(device: WorldDevice) -> bool:
        worlds.mark(device.name, await device.effective_mode())
        return True

    worlds = _worlds_over(_registry(lh), rebuild)

    await worlds.bring_up_to_date("lh")

    assert await worlds.owes_a_rebuild(lh), "a change landed after the rebuild read the record"


@pytest.mark.asyncio
async def test_a_rebuild_that_fails_raises_and_leaves_the_world_marked() -> None:
    lh = LiquidHandler("lh")

    async def rebuild(device: WorldDevice) -> bool:
        raise RuntimeError("deck refused")

    worlds = _worlds_over(_registry(lh), rebuild)

    with pytest.raises(RuntimeError, match="deck refused"):
        await worlds.bring_up_to_date("lh")

    assert await worlds.owes_a_rebuild(lh)


@pytest.mark.asyncio
async def test_an_arm_that_could_not_rebuild_yet_is_rebuilt_at_a_later_command() -> None:
    arm = Transporter("arm")
    calls = 0

    async def holding_a_plate(device: WorldDevice) -> bool:
        nonlocal calls
        calls += 1
        return False

    worlds = _worlds_over(_registry(arm), holding_a_plate)

    await worlds.bring_up_to_date("arm")
    await worlds.bring_up_to_date("arm")

    assert calls == 2
    assert await worlds.owes_a_rebuild(arm)


@pytest.mark.asyncio
async def test_a_deck_grippers_command_rebuilds_its_handler() -> None:
    lh = LiquidHandler("lh")
    rebuilt: list[str] = []

    async def rebuild(device: WorldDevice) -> bool:
        rebuilt.append(device.name)
        return True

    worlds = _worlds_over(_registry(lh, DeckGripperTransporter(lh)), rebuild)

    await worlds.bring_up_to_date("lh/gripper")

    assert rebuilt == ["lh"]


@pytest.mark.asyncio
async def test_marking_the_other_worlds_leaves_the_world_in_force_alone() -> None:
    lh = LiquidHandler("lh")

    async def rebuild(device: WorldDevice) -> bool:
        return True

    worlds = _worlds_over(_registry(lh), rebuild)
    for mode in WorkflowRunMode:
        with mode_scope(mode):
            await worlds.bring_up_to_date("lh")

    with mode_scope(WorkflowRunMode.LIVE):
        await worlds.mark_other_worlds()
        assert not await worlds.owes_a_rebuild(lh)
    with mode_scope(WorkflowRunMode.CHECK_RUN):
        assert await worlds.owes_a_rebuild(lh)


@pytest.mark.asyncio
async def test_a_failed_command_marks_a_handlers_deck_and_leaves_an_arms_world_to_its_recovery() -> None:
    lh, arm = LiquidHandler("lh"), Transporter("arm")

    async def rebuild(device: WorldDevice) -> bool:
        return True

    worlds = _worlds_over(_registry(lh, arm), rebuild)
    await worlds.bring_up_to_date("lh")
    await worlds.bring_up_to_date("arm")

    await worlds.mark_current_world("lh")
    await worlds.mark_current_world("arm")

    assert await worlds.owes_a_rebuild(lh)
    assert not await worlds.owes_a_rebuild(arm), "an arm world holds one plate per site; a mid-run rebuild drops one"


@pytest.mark.asyncio
async def test_a_command_sent_in_one_world_marks_the_devices_other_worlds_and_not_its_own() -> None:
    lh = LiquidHandler("lh")
    set_to(lh, "lh", DeviceMode.LIVE)

    async def rebuild(device: WorldDevice) -> bool:
        return True

    worlds = _worlds_over(_registry(lh), rebuild)
    for mode in (WorkflowRunMode.SIM, WorkflowRunMode.LIVE):
        with mode_scope(mode):
            await worlds.bring_up_to_date("lh")

    with mode_scope(WorkflowRunMode.SIM):
        await worlds.sent_to("lh")
        assert not await worlds.owes_a_rebuild(lh)
    with mode_scope(WorkflowRunMode.LIVE):
        assert await worlds.owes_a_rebuild(lh), "a sim run pipetted; the live deck still holds the old volumes"


@pytest.mark.asyncio
async def test_labware_no_slot_points_at_marks_every_deck_and_leaves_an_arms_world_in_force_alone() -> None:
    lh, arm = LiquidHandler("lh"), Transporter("arm")
    set_to(lh, "lh", DeviceMode.LIVE)
    set_to(arm, "arm", DeviceMode.LIVE)

    async def rebuild(device: WorldDevice) -> bool:
        return True

    worlds = _worlds_over(_registry(lh, arm), rebuild)
    for mode in WorkflowRunMode:
        with mode_scope(mode):
            await worlds.bring_up_to_date("lh")
            await worlds.bring_up_to_date("arm")

    with mode_scope(WorkflowRunMode.SIM):
        await worlds.mark_every_deck_and_other_world()
        assert await worlds.owes_a_rebuild(lh)
        assert not await worlds.owes_a_rebuild(arm), "rebuilding an arm mid-run drops a plate that shares a site"
    with mode_scope(WorkflowRunMode.LIVE):
        assert await worlds.owes_a_rebuild(arm)


class _BroughtUpArm(Transporter):
    @property
    def is_initialized(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_a_record_not_saved_while_rebuilding_after_a_bring_up_reaches_the_caller() -> None:
    """A failed save is never logged and dropped, even where a failed rebuild only leaves the world marked."""
    arm = _BroughtUpArm("arm")

    async def not_saved(device: WorldDevice) -> bool:
        raise CommandNotSavedError.of("arm", "reset_world", OSError("disk full"))

    worlds = _worlds_over(_registry(arm), not_saved)

    with pytest.raises(CommandNotSavedError):
        await worlds.after_a_lifecycle_command("arm")


# -- The pipeline ----------------------------------------------------------------


class _Worlds:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.marked: list[str] = []

    async def bring_up_to_date(self, device_name: str) -> None:
        self.order.append("world")

    async def mark_current_world(self, device_name: str) -> None:
        self.order.append("marked")
        self.marked.append(device_name)

    async def sent_to(self, device_name: str) -> None:
        self.order.append("others marked")

    async def after_a_lifecycle_command(self, device_name: str) -> None:
        self.order.append("after lifecycle")


def _pipeline(order: list[str]) -> tuple[DeviceCommandPipeline[None], _Worlds]:
    pipeline: DeviceCommandPipeline[None] = DeviceCommandPipeline([])
    worlds = _Worlds(order)
    pipeline.keep_worlds_with(worlds)
    return pipeline, worlds


async def _run(
    pipeline: DeviceCommandPipeline[None], order: list[str], *,
    send: Callable[[], Awaitable[str]] | None = None,
    record: Callable[[str], Awaitable[None]] | None = None,
    check: Callable[[], Awaitable[None]] | None = None,
) -> str:
    async def sent() -> str:
        order.append("send")
        return "ok"

    async def recorded(_answer: str) -> None:
        return None

    async def checked() -> None:
        order.append("check")

    return await pipeline.run(
        "lh", "aspirate", send or sent, record or recorded, statement=None, checks=(check or checked,),
        params=None, execution_id=None, events=None, carries_out=None,
    )


@pytest.mark.asyncio
async def test_the_world_is_brought_up_to_date_before_the_checks_and_the_send() -> None:
    order: list[str] = []
    pipeline, worlds = _pipeline(order)

    await _run(pipeline, order)

    assert order == ["world", "check", "send", "others marked"]
    assert worlds.marked == []


async def _initialize(pipeline: DeviceCommandPipeline[None], order: list[str]) -> None:
    async def sent() -> None:
        order.append("send")

    async def recorded(_answer: None) -> None:
        order.append("record")

    await pipeline.run(
        "lh", "initialize", sent, recorded, statement=None, checks=(), params=None, execution_id=None, events=None,
        carries_out=None,
    )


@pytest.mark.asyncio
async def test_a_lifecycle_command_is_sent_with_no_rebuild_before_it_and_its_world_is_rebuilt_after_it() -> None:
    order: list[str] = []
    pipeline, _ = _pipeline(order)

    await _initialize(pipeline, order)

    assert order == ["send", "others marked", "record", "after lifecycle"]


@pytest.mark.asyncio
async def test_a_sent_command_that_failed_marks_its_world() -> None:
    order: list[str] = []
    pipeline, worlds = _pipeline(order)

    async def fails() -> str:
        raise RuntimeError("the driver raised")

    with pytest.raises(RuntimeError):
        await _run(pipeline, order, send=fails)

    assert worlds.marked == ["lh"]


@pytest.mark.asyncio
async def test_a_command_refused_by_its_checks_was_never_sent_and_marks_nothing() -> None:
    order: list[str] = []
    pipeline, worlds = _pipeline(order)

    async def refuses() -> None:
        raise ValueError("the rack is empty")

    with pytest.raises(ValueError):
        await _run(pipeline, order, check=refuses)

    assert worlds.marked == []


@pytest.mark.asyncio
async def test_a_failed_command_marks_its_world_before_the_device_is_let_go() -> None:
    """Another command could take the device the moment it is let go, and must find the world marked."""
    order: list[str] = []
    pipeline, worlds = _pipeline(order)

    @asynccontextmanager
    async def holding() -> AsyncIterator[None]:
        yield
        order.append("let go")

    async def fails() -> str:
        raise RuntimeError("the driver raised")

    with pytest.raises(RuntimeError):
        await pipeline.run(
            "lh", "aspirate", fails, _nothing_recorded, statement=None, checks=(), params=None,
            execution_id=None, events=None, carries_out=None, hold=holding(),
        )

    assert order.index("marked") < order.index("let go")


async def _nothing_recorded(_answer: str) -> None:
    return None


@pytest.mark.asyncio
async def test_a_command_whose_record_was_not_saved_marks_its_world() -> None:
    order: list[str] = []
    pipeline, worlds = _pipeline(order)

    async def not_saved(_answer: str) -> None:
        raise OSError("disk full")

    with pytest.raises(CommandNotSavedError):
        await _run(pipeline, order, record=not_saved)

    assert worlds.marked == ["lh"], "the driver holds what the command did and the record does not"


# -- The whole system ------------------------------------------------------------


async def _sim_deck_site_of(driver: RecordingHandler, name: str) -> str | None:
    state = await driver.get_deck_state(GetDeckStateRequest())
    sites = [item.site for item in state.labware if item.name == name]
    return sites[0] if sites else None


@pytest.mark.out_of_run
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_plate_moved_in_one_world_is_at_its_new_site_and_gone_from_its_old_one_in_the_other() -> None:
    """The rebuild removes what the ledger no longer places on a deck as well as adding what it does."""
    live, sim = RecordingHandler(), RecordingHandler()
    build, _ = await _build(live, sim)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("resident_reagent_wf", mode=WorkflowRunMode.SIM)
        await run_to_quiescence(runtime, record.id)
        moved = [r for r in await runtime.labware.list_all() if named_for_template(r.name, "reservoir")][0]
        assert await _sim_deck_site_of(sim, moved.name) == RESERVOIR_SITE.split("/")[1]

        await runtime.labware.edit_location(moved.id, FREE_SITE, reason="moved it by hand", confirm=True)
        with mode_scope(WorkflowRunMode.SIM):
            await runtime.system.device_worlds.bring_up_to_date("lh")

        assert await _sim_deck_site_of(sim, moved.name) == FREE_SITE.split("/")[1]
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_run_whose_world_cannot_be_rebuilt_is_refused_at_its_start() -> None:
    live, sim = RecordingHandler(), RecordingHandler()
    build, _ = await _build(live, sim)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()

    async def deck_refused(_device: WorldDevice) -> bool:
        raise RuntimeError("the driver refused the deck")

    try:
        runtime.system.device_worlds._rebuild = deck_refused
        with pytest.raises(DevicesNotReadyError):
            await runtime.submit_workflow("resident_reagent_wf", mode=WorkflowRunMode.SIM)
    finally:
        await runtime.shutdown()


@pytest.mark.out_of_run
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_device_that_connected_again_is_rebuilt_now_in_the_world_in_force_and_marked_in_the_others() -> None:
    live, sim = RecordingHandler(), RecordingHandler()
    build, _ = await _build(live, sim)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        worlds = runtime.system.device_worlds
        lh = runtime.system.get_device("lh")
        assert isinstance(lh, LiquidHandler)
        for mode in (WorkflowRunMode.SIM, WorkflowRunMode.LIVE):
            with mode_scope(mode):
                await worlds.bring_up_to_date("lh")
                assert not await worlds.owes_a_rebuild(lh)

        await runtime._on_gateway_device_connected(
            DeviceConnectInfo(name="lh", type="liquid_handler", interfaces=frozenset({"ILiquidHandler"})), "lab_pc",
        )
        await asyncio.gather(*runtime._deck_reseed_tasks)

        with mode_scope(WorkflowRunMode.LIVE):
            assert not await worlds.owes_a_rebuild(lh), "the world in force is pushed when the device comes back"
        with mode_scope(WorkflowRunMode.SIM):
            assert await worlds.owes_a_rebuild(lh)
    finally:
        await runtime.shutdown()
