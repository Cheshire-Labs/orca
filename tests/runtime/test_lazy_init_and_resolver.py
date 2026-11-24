"""Tests for lazy device initialization.

Covers four contracts:

1. ``DeviceRunMode`` resolves each device's mode from the run mode and
   the device's sim or live setting. Only CHECK_RUN picks the in-process sim
   driver; SIM and LIVE both use the live driver slot.
2. ``DeviceFacade._snapshot`` and ``RegistryFacade._device_snapshot`` answer
   with the same base, so one device cannot read two ways depending on which
   route a caller took. That base is the operator write world the row's own
   ``is_initialized`` describes, under the device's setting; it is
   deliberately NOT whatever submission happens to be running.
3. JOIN_EXISTING submissions whose ``run_mode`` differs from the running
   execution's are refused with ``OtherRunModeActiveError``: a system runs
   in one run mode at a time.
4. ``System.ensure_runtime_initialized`` lazily configures LiquidHandler
   decks and initializes device worlds on first execution entry, once
   per run mode and once per resolved device world within it: each
   mode's walk touches a different world, so a CHECK_RUN dry run must not
   satisfy a later LIVE run. Idempotent across concurrent callers within
   a mode. ``SystemRuntime.start`` no longer triggers any device dispatch.
"""

from orca.config import OrcaConfig
import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from unittest.mock import AsyncMock

import pytest
from orca.operations.device_registry import registry_view
from orca.operations.wire_models import EngineDeviceStateDTO

import orca.orca as orca
from cheshire_drivers import (
    DeckLayoutConfig, DeckResourceConfig,
)
from cheshire_drivers.liquid_handler_models import LabwareStateResponse
from orca.devices.devices import LiquidHandler
from orca.events.event_bus import EventBus
from orca.resource_models.devices import Device
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.device_run_mode import DeviceRunMode
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import (
    OUT_OF_RUN_BASE, WorkflowRunMode, current_run_mode,
)
from orca.runtime.errors import OtherRunModeActiveError
from orca.workflow_models.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.resource_models.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from tests.gateway.mode_doubles import unseeded_await
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_templates import WorkflowTemplate

from tests.device_modes import InMemoryDeviceModes, set_to
from tests.mock import EXTERNAL_MOVER, UniversalMockDevice, UniversalSimDriver
from tests.test_helpers import (
    execution_outcome,
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    seeded,
    wire_system_map,
)
from tests.setup_package import package_stores, write_deck_layouts
from tests.setup_package import with_deck_layouts
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.mock import outside_any_system
from tests.real_device_path import the_real_device_path
from tests.recording_handler import RecordingHandler
from tests.memory_activity import memory_labware_service


pytestmark = pytest.mark.asyncio


def _manager(setting: DeviceMode) -> DeviceRunMode:
    manager = DeviceRunMode("dev")
    manager.use_device_modes(InMemoryDeviceModes({"dev": setting}))
    return manager


class TestDeviceRunModeResolver:
    """A run mode and a device's sim or live setting pick the mode its commands carry."""

    async def test_a_check_run_decides_for_every_device(self) -> None:
        manager = _manager(DeviceMode.LIVE)
        with seeded(WorkflowRunMode.CHECK_RUN):
            assert await manager.effective_mode() is WorkflowRunMode.CHECK_RUN

    async def test_live_submission_device_set_to_live_resolves_live(self) -> None:
        manager = _manager(DeviceMode.LIVE)
        with seeded(WorkflowRunMode.LIVE):
            assert await manager.effective_mode() is WorkflowRunMode.LIVE

    async def test_live_submission_device_set_to_sim_resolves_sim(self) -> None:
        """orca-client reads effective_mode SIM and uses its simulator."""
        manager = _manager(DeviceMode.SIM)
        with seeded(WorkflowRunMode.LIVE):
            assert await manager.effective_mode() is WorkflowRunMode.SIM

    async def test_sim_submission_device_set_to_live_resolves_sim(self) -> None:
        manager = _manager(DeviceMode.LIVE)
        with seeded(WorkflowRunMode.SIM):
            assert await manager.effective_mode() is WorkflowRunMode.SIM

    async def test_a_device_with_no_setting_attached_is_sim(self) -> None:
        manager = DeviceRunMode("dev")
        assert await manager.setting() is DeviceMode.SIM
        assert await manager.mode_under(WorkflowRunMode.LIVE) is WorkflowRunMode.SIM


async def _listed_engine(runtime: SystemRuntime, name: str) -> EngineDeviceStateDTO:
    """The engine's state for one device as the device registry list shows it."""
    rows = {e.name: await registry_view(runtime, e) for e in await runtime.device_registry.list_all()}
    engine = rows[name].engine
    assert engine is not None
    return engine


class TestSnapshotResolver:
    """Both snapshot facades answer one question the same way.

    A device row describes the world an operator's own verbs act in, under the
    device's own setting. Nothing else moves it: not the caller, and not a
    submission that happens to be running.
    """

    async def test_a_declared_sim_device_reads_sim_on_the_per_device_route(self) -> None:
        """A device set to sim never reads live, though operator verbs resolve the LIVE base."""
        plate = create_test_plate_template("plate_96")
        sim_device = create_test_device("shaker1", setting=DeviceMode.SIM)
        transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
        registry = ResourceRegistry()
        registry.add_resource(sim_device)
        registry.add_resource(transporter)
        pool = ResourcePool("shaker1", [sim_device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker1": sim_device}, pads=["pad1"])

        builder = SdkToSystemBuilder(
            name="t", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map, workflows=[], event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
            labware_catalog=bundled_labware_catalog(),
            labware_service=memory_labware_service(),
            unsaved_positions=memory_unsaved_positions(),
        )
        await builder.bind_labwares()
        runtime = await memory_runtime(builder.get_system(), event_bus=EventBus())
        await runtime.start()
        try:
            snapshot = await unseeded_await(runtime.devices.get_device_status("shaker1"))
            assert snapshot.setting is DeviceMode.SIM
            assert snapshot.effective_mode is WorkflowRunMode.SIM, (
                f"this device is set to sim, so no base a caller states can "
                f"make it read live; got {snapshot.effective_mode!r}."
            )
        finally:
            await runtime.shutdown()

    async def test_a_declared_sim_device_reads_sim_on_the_list_route(self) -> None:
        """The same answer on the list route, so the two cannot disagree."""
        plate = create_test_plate_template("plate_96")
        sim_device = create_test_device("shaker1", setting=DeviceMode.SIM)
        transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
        registry = ResourceRegistry()
        registry.add_resource(sim_device)
        registry.add_resource(transporter)
        pool = ResourcePool("shaker1", [sim_device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker1": sim_device}, pads=["pad1"])

        builder = SdkToSystemBuilder(
            name="t", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map, workflows=[], event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
            labware_catalog=bundled_labware_catalog(),
            labware_service=memory_labware_service(),
            unsaved_positions=memory_unsaved_positions(),
        )
        await builder.bind_labwares()
        runtime = await memory_runtime(builder.get_system(), event_bus=EventBus())
        await runtime.start()
        try:
            shaker = await unseeded_await(_listed_engine(runtime, "shaker1"))
            assert shaker.effective_mode is WorkflowRunMode.SIM
        finally:
            await runtime.shutdown()

    async def _one_shaker_runtime(self) -> SystemRuntime:
        plate = create_test_plate_template("plate_96")
        device = create_test_device("shaker1", setting=DeviceMode.LIVE)
        transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        registry.add_resource_pool(ResourcePool("shaker1", [device]))
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

        builder = SdkToSystemBuilder(
            name="t", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map, workflows=[], event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
            labware_catalog=bundled_labware_catalog(),
            labware_service=memory_labware_service(),
            unsaved_positions=memory_unsaved_positions(),
        )
        await builder.bind_labwares()
        runtime = await memory_runtime(builder.get_system(), event_bus=EventBus())
        await runtime.start()
        return runtime

    @staticmethod
    async def _both_modes(runtime: SystemRuntime) -> tuple[WorkflowRunMode, WorkflowRunMode]:
        from_list = await _listed_engine(runtime, "shaker1")
        return from_list.effective_mode, (await runtime.devices.get_device_status(
            "shaker1"
        )).effective_mode

    async def test_both_device_facades_agree_outside_a_run(self) -> None:
        """Out of a run the row describes the operator's world, on both facades.

        The list route reported the metadata fallback while the per-device route
        reported the operator write base, so the same device answered CHECK_RUN
        or LIVE depending on which one a reader happened to call first.
        """
        runtime = await self._one_shaker_runtime()
        try:
            listed, fetched = await unseeded_await(self._both_modes(runtime))
            assert listed is fetched, (
                f"the device list said {listed!r} and get_device_status said "
                f"{fetched!r} for one device; whichever a reader calls first "
                f"becomes their answer."
            )
            assert listed is OUT_OF_RUN_BASE, (
                f"out of a run a device row describes the world an operator's "
                f"own verbs act in; got {listed!r}."
            )
        finally:
            await runtime.shutdown()

    async def test_a_run_in_force_does_not_change_what_a_device_row_says(self) -> None:
        """The row describes the operator's world, run or no run.

        Its `is_initialized` is read through `DeviceLinkReader`, which resolves
        the operator write base so a `connect` or `initialize` moves a flag the
        reader can see. Letting the mode follow a submission instead would put
        a sim-world mode beside a live-world flag in the same row.
        """
        runtime = await self._one_shaker_runtime()
        try:
            token = current_run_mode.set(WorkflowRunMode.CHECK_RUN)
            try:
                listed, fetched = await self._both_modes(runtime)
            finally:
                current_run_mode.reset(token)
            assert listed is fetched
            assert listed is OUT_OF_RUN_BASE, (
                f"a CHECK_RUN submission is in force but the row still describes "
                f"the operator world its flags describe; got {listed!r}."
            )
        finally:
            await runtime.shutdown()


class TestRunModeMismatch:
    """JOIN_EXISTING refuses submissions whose `run_mode` differs from the live execution.

    Replaces the v3.4 ``CONCURRENT_SUBMISSION_REFUSED`` refuse-all with a
    per-execution check. STANDALONE submissions always proceed (each
    boots its own fresh execution with its own ContextVar seed);
    JOIN_EXISTING submissions inherit the existing execution's mode and
    therefore must match it.
    """

    async def _build_runtime_with_join_workflow(self) -> SystemRuntime:
        """Build a one-shaker runtime with a thread template that goes
        through the JOIN_EXISTING path (i.e. groups + active_executions).
        """
        plate = create_test_plate_template("plate_96")
        device = UniversalMockDevice("shaker1")
        transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("shaker1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate])
        async def shake_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def shake_method(
            ctx: MethodContext,
        ) -> AsyncGenerator[ActionTemplate, None]:
            del ctx
            yield shake_action

        pad1 = system_map.get_location("pad1")

        @orca.thread(labware=plate, start=pad1, end=pad1)
        async def plate_thread(
            ctx: ThreadContext,
        ) -> AsyncGenerator[MethodTemplate, None]:
            del ctx
            yield shake_method

        workflow = WorkflowTemplate("rmm_test")
        workflow.add_thread(plate_thread, is_start=True)

        bus = EventBus()
        builder = SdkToSystemBuilder(
            name="rmm_sys", description="",
            labwares=[plate], resources_registry=registry,
            system_map=system_map, workflows=[workflow], event_bus=bus, config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
            labware_catalog=bundled_labware_catalog(),
            labware_service=memory_labware_service(),
            unsaved_positions=memory_unsaved_positions(),
        )
        await builder.bind_labwares()
        runtime = await memory_runtime(builder.get_system(), event_bus=bus)
        await runtime.start()
        return runtime

    async def test_standalone_concurrent_same_mode_accepted(self) -> None:
        """Concurrent STANDALONE same-mode submissions each get their own execution."""
        runtime = await self._build_runtime_with_join_workflow()
        try:
            workflow = runtime.system.get_workflow_template("rmm_test")
            s1 = await runtime.submit(
                workflow,
                mode=WorkflowRunMode.CHECK_RUN,
                batch_mode=BatchMode.STANDALONE,
            )
            s2 = await runtime.submit(
                workflow,
                mode=WorkflowRunMode.CHECK_RUN,
                batch_mode=BatchMode.STANDALONE,
            )
            assert s1.execution_id != s2.execution_id, (
                "STANDALONE submissions must always boot fresh executions."
            )
            # No completion wait: both plates seed the ONE pad, so neither
            # execution can finish here. Acceptance is the test; shutdown aborts.
        finally:
            await runtime.shutdown()

    async def test_join_existing_mismatched_mode_refused(self) -> None:
        """A JOIN_EXISTING in another run mode is refused by the one-run-mode-at-a-time rule."""
        runtime = await self._build_runtime_with_join_workflow()
        try:
            workflow = runtime.system.get_workflow_template("rmm_test")
            group_a = LabwareGroup(
                id="g_a",
                members=(
                    LabwareGroupMember(thread_template_name="plate_thread"),
                ),
            )
            group_b = LabwareGroup(
                id="g_b",
                members=(
                    LabwareGroupMember(thread_template_name="plate_thread"),
                ),
            )
            s1 = await runtime.submit(
                workflow,
                groups=[group_a],
                mode=WorkflowRunMode.CHECK_RUN,
                batch_mode=BatchMode.STANDALONE,
            )
            with pytest.raises(OtherRunModeActiveError) as excinfo:
                await runtime.submit(
                    workflow,
                    groups=[group_b],
                    mode=WorkflowRunMode.LIVE,
                    batch_mode=BatchMode.JOIN_EXISTING,
                )
            assert excinfo.value.existing_run_mode is WorkflowRunMode.CHECK_RUN
            assert excinfo.value.submitted_run_mode is WorkflowRunMode.LIVE
            assert excinfo.value.blocking_execution_id == s1.execution_id
            await execution_outcome(runtime, s1, timeout=30.0)
        finally:
            await runtime.shutdown()


class CountingInitDriver(UniversalSimDriver):
    """Counts initialize dispatches so tests can observe device init per world."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.init_calls = 0

    async def initialize(self) -> None:
        self.init_calls += 1
        await super().initialize()


class FailingOnceInitDriver(CountingInitDriver):
    """First initialize raises; later attempts succeed."""

    async def initialize(self) -> None:
        self.init_calls += 1
        if self.init_calls == 1:
            raise RuntimeError(f"{self.name}: init failed once")
        await UniversalSimDriver.initialize(self)


async def _build_lazy_runtime(
    devices: Mapping[str, Device],
    teach: Sequence[str] = ("pad1",),
    plate: PlateTemplate | None = None,
) -> SystemRuntime:
    """One construction path for every lazy-init test: registry + map wiring
    + SdkToSystemBuilder, differing only in the resource set."""
    plate = plate or create_test_plate_template("plate_96")
    transporter = create_test_transporter("robot1", list(teach))
    registry = ResourceRegistry()
    for device in devices.values():
        registry.add_resource(device)
    registry.add_resource(transporter)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices=dict(devices), pads=["pad1"])
    builder = SdkToSystemBuilder(
        name="lazy_sys", description="",
        labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[], event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return await memory_runtime(builder.get_system(), event_bus=EventBus())


class TestLazyRuntimeInitialization:
    """`System.ensure_runtime_initialized` lazy first-thread-touch seam.

    Device init and the deck-config push defer to
    `System.ensure_runtime_initialized`, which executes on first execution
    entry inside a seeded `current_run_mode` task -- once per run mode,
    and once per resolved device world within it.
    """

    async def _runtime_with_counting_device(
        self,
    ) -> tuple[SystemRuntime, CountingInitDriver]:
        driver = CountingInitDriver("shaker1")
        device = UniversalMockDevice("shaker1", driver=driver)
        runtime = await _build_lazy_runtime(
            {"shaker1": device}, teach=("shaker1", "pad1"),
        )
        return runtime, driver

    async def test_runtime_start_does_not_initialize_devices(self) -> None:
        """`runtime.start()` no longer triggers device init under any mode.

        The deferral is what unblocks LIVE workflows in test fixtures
        without a real gateway -- pre-fix, `runtime.start()` walked
        every LiquidHandler at boot regardless of run_mode.
        """
        runtime, driver = await self._runtime_with_counting_device()
        try:
            await runtime.start()
            assert driver.init_calls == 0, (
                f"runtime.start() must defer device init to first-thread-touch; "
                f"observed {driver.init_calls} initialize dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_a_check_run_brings_its_devices_up_as_a_sim_run_does(self) -> None:
        runtime, driver = await self._runtime_with_counting_device()
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.CHECK_RUN):
                await runtime.system.ensure_runtime_initialized()
            assert driver.init_calls == 1, (
                f"a check run must initialize its devices once; observed "
                f"{driver.init_calls} dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_live_entry_initializes_devices_once(self) -> None:
        runtime, driver = await self._runtime_with_counting_device()
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
            assert driver.init_calls == 1, (
                f"LIVE run mode must initialize devices exactly once; observed "
                f"{driver.init_calls} dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_ensure_runtime_initialized_is_idempotent_within_a_mode(
        self,
    ) -> None:
        """Concurrent + serial repeat calls under one mode run the walk once."""
        runtime, driver = await self._runtime_with_counting_device()
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                await asyncio.gather(
                    runtime.system.ensure_runtime_initialized(),
                    runtime.system.ensure_runtime_initialized(),
                    runtime.system.ensure_runtime_initialized(),
                )
                await runtime.system.ensure_runtime_initialized()
            assert driver.init_calls == 1, (
                f"Lazy seam must be idempotent across concurrent + serial "
                f"calls; observed {driver.init_calls} dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_live_after_check_run_still_initializes_devices(self) -> None:
        """A CHECK_RUN dry run must not consume the LIVE run's device init.

        The operator sequence this protects: a dry run first, then go live.
        The LIVE entry that follows must still initialize the real devices,
        not observe a mode-blind done flag.
        """
        runtime, driver = await self._runtime_with_counting_device()
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.CHECK_RUN):
                await runtime.system.ensure_runtime_initialized()
            assert driver.init_calls == 1
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
            assert driver.init_calls == 2, (
                f"LIVE entry after a CHECK_RUN run must still initialize "
                f"devices; observed {driver.init_calls} dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_each_device_world_is_initialized_once(self) -> None:
        """Init keys on the resolved per-device world, mirroring deck config.

        A device set to sim resolves SIM under a SIM and a LIVE run, so it is
        initialized once; a device set to live is initialized once per mode
        because the device bridge swaps its backend per stamped mode.
        """
        pinned_driver = CountingInitDriver("pinned")
        pinned = UniversalMockDevice("pinned", driver=pinned_driver)
        set_to(pinned, "pinned", DeviceMode.SIM)
        wired_driver = CountingInitDriver("wired")
        wired = UniversalMockDevice("wired", driver=wired_driver)
        set_to(wired, "wired", DeviceMode.LIVE)
        runtime = await _build_lazy_runtime(
            {"pinned": pinned, "wired": wired},
            teach=("pinned", "wired", "pad1"),
        )
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.SIM):
                await runtime.system.ensure_runtime_initialized()
            assert pinned_driver.init_calls == 1
            assert wired_driver.init_calls == 1
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
            assert pinned_driver.init_calls == 1, (
                "The sim device's world did not change; a second "
                "submission mode must not re-initialize it."
            )
            assert wired_driver.init_calls == 2, (
                "SIM and LIVE are different wire backends; each "
                "needs its own init."
            )
        finally:
            await runtime.shutdown()

    async def test_a_device_switched_to_live_is_brought_up_before_the_next_live_run(self) -> None:
        driver = CountingInitDriver("arm")
        arm = UniversalMockDevice("arm", driver=driver)
        runtime = await _build_lazy_runtime({"arm": arm}, teach=("arm", "pad1"))
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
            assert driver.init_calls == 1

            set_to(arm, "arm", DeviceMode.LIVE)
            runtime.system.forget_bringup("arm")
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.bring_up_device("arm")
            assert driver.init_calls == 2, "the real device must be brought up before it takes a command"
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()

            assert driver.init_calls == 2, "the next run finds it already up in that mode"
        finally:
            await runtime.shutdown()

    async def test_failed_init_retries_only_the_failed_device(self) -> None:
        """Init done-ness is marked per device world, so a retry after a
        partial failure does not re-home the devices that succeeded."""
        ok_driver = CountingInitDriver("ok_dev")
        ok_device = UniversalMockDevice("ok_dev", driver=ok_driver)
        flaky_driver = FailingOnceInitDriver("flaky_dev")
        flaky_device = UniversalMockDevice("flaky_dev", driver=flaky_driver)
        runtime = await _build_lazy_runtime(
            {"ok_dev": ok_device, "flaky_dev": flaky_device},
            teach=("ok_dev", "flaky_dev", "pad1"),
        )
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                with pytest.raises(RuntimeError, match="init failed once"):
                    await runtime.system.ensure_runtime_initialized()
                await runtime.system.ensure_runtime_initialized()
            assert ok_driver.init_calls == 1, (
                "The device that initialized cleanly must not be brought up "
                "again by the retry."
            )
            assert flaky_driver.init_calls == 2
        finally:
            await runtime.shutdown()


LH_DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(
            name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7,
        ),
    ],
)


async def _build_recorded_lh(
    setting: DeviceMode = DeviceMode.SIM,
) -> tuple[LiquidHandler, list[WorkflowRunMode]]:
    """A LiquidHandler, and the world each configure_deck it sends lands in."""
    driver = RecordingHandler()
    stores = package_stores()
    the_real_device_path().simulate_with("lh", driver)
    lh = with_deck_layouts(LiquidHandler(
        "lh",
        deck_layout="default",
    ), write_deck_layouts(stores, "lh", {"default": LH_DECK_CONFIG},
        ))
    set_to(outside_any_system(lh), "lh", setting)
    configured: list[WorkflowRunMode] = []
    configure = driver.configure_deck

    async def configure_in_its_world(config: DeckLayoutConfig) -> LabwareStateResponse:
        configured.append(await lh.effective_mode())
        return await configure(config)

    setattr(driver, "configure_deck", configure_in_its_world)
    await the_real_device_path().connect_devices(lh)
    return lh, configured


class TestDeckConfiguredPerWorld:
    """`LiquidHandler.deck_world_layout` lays out each resolved world.

    orca-client's simulator and its real driver are separate worlds: a
    configure_deck sent as SIM leaves the live instrument bare. The done-gate
    must therefore key on the resolved per-device mode, not a process-wide
    boolean.
    """

    async def test_each_world_gets_its_own_configure(self) -> None:
        lh, configured = await _build_recorded_lh(DeviceMode.LIVE)

        with seeded(WorkflowRunMode.SIM):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, True)
        assert configured == [WorkflowRunMode.SIM]

        with seeded(WorkflowRunMode.LIVE):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, True)
        assert configured == [WorkflowRunMode.SIM, WorkflowRunMode.LIVE], (
            "The LIVE world was never configured; the SIM configure must not satisfy it."
        )

        with seeded(WorkflowRunMode.LIVE):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, False)
        assert configured == [WorkflowRunMode.SIM, WorkflowRunMode.LIVE], (
            "A second LIVE call must observe the LIVE world already configured."
        )

    async def test_a_sim_setting_pins_the_configured_world(self) -> None:
        """A device set to sim resolves SIM under a SIM and a LIVE run, so the
        second call is a no-op on an already-configured world."""
        lh, configured = await _build_recorded_lh(DeviceMode.SIM)

        with seeded(WorkflowRunMode.SIM):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, True)
        with seeded(WorkflowRunMode.LIVE):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, False)
        assert configured == [WorkflowRunMode.SIM]

    async def test_a_live_setting_gets_a_configure_per_wire_world(self) -> None:
        """A device set to live resolves SIM under a SIM run and LIVE under a
        LIVE run; orca-client swaps backends between them, so each is configured."""
        lh, configured = await _build_recorded_lh(DeviceMode.LIVE)

        with seeded(WorkflowRunMode.SIM):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, True)
        with seeded(WorkflowRunMode.LIVE):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, True)
        assert configured == [WorkflowRunMode.SIM, WorkflowRunMode.LIVE]

    async def test_concurrent_callers_configure_a_world_once(self) -> None:
        """The walk and a facade-triggered reconcile can race into the
        configure; the device-level lock must collapse them to one dispatch."""
        lh, configured = await _build_recorded_lh()
        with seeded(WorkflowRunMode.LIVE):
            results = await asyncio.gather(
                lh.deck_world_layout(),
                lh.deck_world_layout(),
                lh.deck_world_layout(),
            )
        assert len(configured) == 1, (
            f"Concurrent callers dispatched {len(configured)} "
            f"configure_deck calls; the lock must allow exactly one."
        )
        laid_out = [result for result in results if result is not None]
        assert len(laid_out) == 3
        assert sum(1 for _layout, fresh in laid_out if fresh) == 1, (
            "Exactly one caller must report a fresh configure."
        )

    async def test_reset_in_one_world_keeps_the_other_worlds_identity_guard(
        self,
    ) -> None:
        """`reset_labware_state` under one mode must not disarm the other
        world's DeckLabwareIdentityError guard: the instance map is per
        world, like the deck it mirrors."""
        lh, _ = await _build_recorded_lh(DeviceMode.LIVE)
        plate = PlateTemplate(
            "guard_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        )
        runtime = await _build_lazy_runtime({"lh": lh}, plate=plate)
        try:
            await runtime.start()
            template = runtime.system.get_labware_template("guard_plate")
            first = await template.create_instance()
            await first.enter_record(runtime.system.labware_contents)
            with seeded(WorkflowRunMode.LIVE):
                await lh.deck_world_layout()
                await lh._do_notify_placed(first, EXTERNAL_MOVER, target="carrier-7-0")
            with seeded(WorkflowRunMode.SIM):
                await lh.deck_world_layout()
                await lh.reset_labware_state()
            # One in-process driver stands in for both of orca-client's worlds here, so the
            # check is on the per-world record the identity guard reads.
            assert lh._instances_in(WorkflowRunMode.LIVE) == {first.name: first.id}
            assert lh._instances_in(WorkflowRunMode.SIM) == {}
        finally:
            await runtime.shutdown()


class TestLazyInitWalkReconcilesPerWorld:
    """The lazy-init walk reconciles a deck world once, when it is fresh.

    A second submission mode that resolves an LH to an already-configured
    world (a device set to sim, under a SIM then a LIVE submission) must not
    re-reconcile it: reconcile is a clear+rebuild that resets occupant
    volume/tip state, so re-running it on a world kept in sync since its
    first configure would wipe accumulated state. A FAILED reconcile,
    though, stays owed: the next entry must retry it even though the
    configure itself is not repeated.
    """

    async def test_sim_set_lh_reconciled_once_across_modes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lh, configured = await _build_recorded_lh(DeviceMode.SIM)
        runtime = await _build_lazy_runtime({"lh": lh})
        system = runtime.system
        reconcile = AsyncMock()
        monkeypatch.setattr(system.labware_placer, "deck_rebuilt", reconcile)
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.SIM):
                await system.ensure_runtime_initialized()
            assert configured == [WorkflowRunMode.SIM]
            assert reconcile.await_count == 1

            with seeded(WorkflowRunMode.LIVE):
                await system.ensure_runtime_initialized()
            assert configured == [WorkflowRunMode.SIM], (
                "The sim-set LH resolves to the already-configured SIM "
                "world; the LIVE walk must not reconfigure it."
            )
            assert reconcile.await_count == 1, (
                f"An already-reconciled world must not be re-reconciled "
                f"(state reset); observed {reconcile.await_count} reconciles."
            )
        finally:
            await runtime.shutdown()

    async def test_failed_reconcile_is_retried_on_the_next_entry(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fresh world whose reconcile failed keeps the obligation: the
        retry walk must reconcile it even though configure is not repeated."""
        lh, configured = await _build_recorded_lh()
        runtime = await _build_lazy_runtime({"lh": lh})
        system = runtime.system
        reconcile = AsyncMock(side_effect=[RuntimeError("wire dropped"), None])
        monkeypatch.setattr(system.labware_placer, "deck_rebuilt", reconcile)
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                with pytest.raises(RuntimeError, match="wire dropped"):
                    await system.ensure_runtime_initialized()
                await system.ensure_runtime_initialized()
            assert len(configured) == 1, (
                "The retry must not re-dispatch configure_deck to a world "
                "that already has its carrier skeleton."
            )
            assert reconcile.await_count == 2, (
                f"The failed reconcile must be retried; observed "
                f"{reconcile.await_count} attempts."
            )
        finally:
            await runtime.shutdown()
