"""Tests for lazy device initialization.

Covers four contracts:

1. ``SimulationManager.driver`` consults the 12-row resolver
   ``resolve_effective_mode_for_device(current_run_mode.get(), sim_override)``
   instead of reading ``current_run_mode`` directly. A LIVE submission
   touching a device with ``sim_override=PURE_SIM`` dispatches against
   the sim driver; a LIVE submission with no override dispatches against
   the live driver.
2. ``DeviceFacade._snapshot`` and ``RegistryFacade._device_snapshot`` answer
   with the same base, so one device cannot read two ways depending on which
   route a caller took. That base is the operator write world the row's own
   ``is_initialized`` describes, with the topology override ratcheted on top;
   it is deliberately NOT whatever submission happens to be running.
3. JOIN_EXISTING submissions whose ``run_mode`` differs from the existing
   execution's stamped ``run_mode`` are refused with
   ``RunModeMismatchError`` / ``RUN_MODE_MISMATCH`` (replacing the
   temporary ``CONCURRENT_SUBMISSION_REFUSED`` refuse-all).
4. ``System.ensure_runtime_initialized`` lazily configures LiquidHandler
   decks and initializes device worlds on first execution entry, once
   per run mode and once per resolved device world within it: each
   mode's walk touches a different world, so a PURE_SIM dry run must not
   satisfy a later LIVE run. Idempotent across concurrent callers within
   a mode. ``SystemRuntime.start`` no longer triggers any device dispatch.
"""

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from unittest.mock import AsyncMock

import pytest

import orca.orca as orca
from cheshire_drivers import (
    DeckLayoutConfig, DeckResourceConfig, RecordingLiquidHandlerDriver,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.devices.devices import DeckLabwareIdentityError, LiquidHandler
from orca.events.event_bus import EventBus
from orca.resource_models.devices import Device
from orca.resource_models.labware import PlateInstance, PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.simulation_manager import SimulationManager
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.sim_labware import SimPlate
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import (
    OPERATOR_DEVICE_WRITE_BASE, WorkflowRunMode, current_run_mode,
)
from orca.runtime.runtime_interface import RunModeMismatchError
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from tests.gateway.mode_doubles import unseeded
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_templates import WorkflowTemplate

from tests.mock import EXTERNAL_MOVER, UniversalMockDevice, UniversalSimDriver
from tests.test_helpers import (
    PairDriverFactory,
    execution_outcome,
    create_test_plate_template,
    create_test_transporter,
    seeded,
    wire_system_map,
)


pytestmark = pytest.mark.asyncio


class TestSimulationManagerResolver:
    """The 12-row resolver gates `SimulationManager.driver`.

    Pre-v3.4 the manager read `current_run_mode` directly and returned
    `_sim_driver` for PURE_SIM, `_live_driver` otherwise. That ignored
    per-device `sim_override`, so a LIVE submission touching an
    override-PURE_SIM device dispatched against the live driver despite
    the override. The follow-up branch closes the gap.
    """

    async def test_pure_sim_submission_returns_sim_driver(self) -> None:
        manager: SimulationManager[str] = SimulationManager(
            live_driver="live", sim_driver="sim", sim_override=None,
        )
        token = current_run_mode.set(WorkflowRunMode.PURE_SIM)
        try:
            assert manager.driver == "sim"
        finally:
            current_run_mode.reset(token)

    async def test_live_submission_no_override_returns_live_driver(self) -> None:
        manager: SimulationManager[str] = SimulationManager(
            live_driver="live", sim_driver="sim", sim_override=None,
        )
        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            assert manager.driver == "live"
        finally:
            current_run_mode.reset(token)

    async def test_live_submission_pure_sim_override_returns_sim_driver(self) -> None:
        """LIVE submission + PURE_SIM device override -> sim driver.

        The 12-row resolver ratchets toward sim. Pre-fix dispatch ignored
        the override and dispatched against `live`.
        """
        manager: SimulationManager[str] = SimulationManager(
            live_driver="live", sim_driver="sim",
            sim_override=WorkflowRunMode.PURE_SIM,
        )
        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            assert manager.driver == "sim"
        finally:
            current_run_mode.reset(token)

    async def test_live_submission_device_sim_override_returns_live_driver(self) -> None:
        """LIVE submission + DEVICE_SIM device override -> live driver locally.

        DEVICE_SIM and LIVE both dispatch against the local live driver;
        the sim/live split for DEVICE_SIM happens on the orca-client wire
        side, not in `SimulationManager`.
        """
        manager: SimulationManager[str] = SimulationManager(
            live_driver="live", sim_driver="sim",
            sim_override=WorkflowRunMode.DEVICE_SIM,
        )
        token = current_run_mode.set(WorkflowRunMode.LIVE)
        try:
            assert manager.driver == "live"
        finally:
            current_run_mode.reset(token)

    async def test_device_sim_submission_pure_sim_override_returns_sim_driver(self) -> None:
        """DEVICE_SIM submission + PURE_SIM override -> sim driver."""
        manager: SimulationManager[str] = SimulationManager(
            live_driver="live", sim_driver="sim",
            sim_override=WorkflowRunMode.PURE_SIM,
        )
        token = current_run_mode.set(WorkflowRunMode.DEVICE_SIM)
        try:
            assert manager.driver == "sim"
        finally:
            current_run_mode.reset(token)


class TestSnapshotResolver:
    """Both snapshot facades answer one question the same way.

    A device row describes the world an operator's own verbs act in, and a
    topology `sim_override` ratchets that answer toward sim. Nothing else moves
    it: not the caller, and not a submission that happens to be running.
    """

    async def test_a_declared_sim_device_reads_sim_on_the_per_device_route(self) -> None:
        """The topology ratchet holds: a device declared sim never reads live."""
        plate = create_test_plate_template("plate_96")
        sim_device = UniversalMockDevice(
            "shaker1", sim_override=WorkflowRunMode.PURE_SIM,
        )
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
            system_map=system_map, workflows=[], event_bus=EventBus(),
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=EventBus())
        await runtime.start()
        try:
            snapshot = unseeded(
                lambda: runtime.devices.get_device_status("shaker1")
            )
            assert snapshot.effective_mode is WorkflowRunMode.PURE_SIM, (
                f"the topology declares this device PURE_SIM, so no base a "
                f"caller states can make it read live; got "
                f"{snapshot.effective_mode!r}."
            )
        finally:
            await runtime.shutdown()

    async def test_a_declared_sim_device_reads_sim_on_the_list_route(self) -> None:
        """The same ratchet, on the list route, so the two cannot disagree."""
        plate = create_test_plate_template("plate_96")
        sim_device = UniversalMockDevice(
            "shaker1", sim_override=WorkflowRunMode.PURE_SIM,
        )
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
            system_map=system_map, workflows=[], event_bus=EventBus(),
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=EventBus())
        await runtime.start()
        try:
            shaker = unseeded(
                lambda: next(
                    d for d in runtime.registry.list_devices()
                    if d.name == "shaker1"
                )
            )
            assert shaker.effective_mode is WorkflowRunMode.PURE_SIM
        finally:
            await runtime.shutdown()

    async def _one_shaker_runtime(self) -> SystemRuntime:
        plate = create_test_plate_template("plate_96")
        device = UniversalMockDevice("shaker1")
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
            system_map=system_map, workflows=[], event_bus=EventBus(),
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=EventBus())
        await runtime.start()
        return runtime

    @staticmethod
    def _both_modes(runtime: SystemRuntime) -> tuple[WorkflowRunMode, WorkflowRunMode]:
        from_list = next(
            d for d in runtime.registry.list_devices() if d.name == "shaker1"
        )
        return from_list.effective_mode, runtime.devices.get_device_status(
            "shaker1"
        ).effective_mode

    async def test_both_device_facades_agree_outside_a_run(self) -> None:
        """Out of a run the row describes the operator's world, on both facades.

        The list route reported the metadata fallback while the per-device route
        reported the operator write base, so the same device answered PURE_SIM
        or LIVE depending on which one a reader happened to call first.
        """
        runtime = await self._one_shaker_runtime()
        try:
            listed, fetched = unseeded(lambda: self._both_modes(runtime))
            assert listed is fetched, (
                f"the device list said {listed!r} and get_device_status said "
                f"{fetched!r} for one device; whichever a reader calls first "
                f"becomes their answer."
            )
            assert listed is OPERATOR_DEVICE_WRITE_BASE, (
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
            token = current_run_mode.set(WorkflowRunMode.PURE_SIM)
            try:
                listed, fetched = self._both_modes(runtime)
            finally:
                current_run_mode.reset(token)
            assert listed is fetched
            assert listed is OPERATOR_DEVICE_WRITE_BASE, (
                f"a PURE_SIM submission is in force but the row still describes "
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
            system_map=system_map, workflows=[workflow], event_bus=bus,
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=bus)
        await runtime.start()
        return runtime

    async def test_standalone_concurrent_same_mode_accepted(self) -> None:
        """Concurrent STANDALONE same-mode submissions each get their own execution."""
        runtime = await self._build_runtime_with_join_workflow()
        try:
            workflow = runtime.system.get_workflow_template("rmm_test")
            s1 = await runtime.submit(
                workflow,
                mode=WorkflowRunMode.PURE_SIM,
                batch_mode=BatchMode.STANDALONE,
            )
            s2 = await runtime.submit(
                workflow,
                mode=WorkflowRunMode.PURE_SIM,
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
        """JOIN_EXISTING with mismatched run_mode raises RunModeMismatchError."""
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
                mode=WorkflowRunMode.PURE_SIM,
                batch_mode=BatchMode.STANDALONE,
            )
            with pytest.raises(RunModeMismatchError) as excinfo:
                await runtime.submit(
                    workflow,
                    groups=[group_b],
                    mode=WorkflowRunMode.LIVE,
                    batch_mode=BatchMode.JOIN_EXISTING,
                )
            assert excinfo.value.existing_run_mode is WorkflowRunMode.PURE_SIM
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
        system_map=system_map, workflows=[], event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return SystemRuntime(builder.get_system(), event_bus=EventBus())


class TestLazyRuntimeInitialization:
    """`System.ensure_runtime_initialized` lazy first-thread-touch seam.

    Pre-Phase-C `SystemRuntime.start()` walked every LiquidHandler in the
    topology and pushed its deck config, and `SystemBuild.run()` also ran
    device init for non-PURE_SIM modes. Both walks now defer to
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

    async def test_pure_sim_entry_skips_device_init(self) -> None:
        """PURE_SIM context: the walk pushes LH decks but initializes nothing."""
        runtime, driver = await self._runtime_with_counting_device()
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.PURE_SIM):
                await runtime.system.ensure_runtime_initialized()
            assert driver.init_calls == 0, (
                f"PURE_SIM run mode must skip device init; observed "
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

    async def test_live_after_pure_sim_still_initializes_devices(self) -> None:
        """A PURE_SIM dry run must not consume the LIVE run's device init.

        The operator sequence this protects: sim dry-run first, then go
        live. The PURE_SIM entry walks the sim world and skips device
        init; the LIVE entry that follows must still initialize the real
        devices, not observe a mode-blind done flag.
        """
        runtime, driver = await self._runtime_with_counting_device()
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.PURE_SIM):
                await runtime.system.ensure_runtime_initialized()
            assert driver.init_calls == 0
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
            assert driver.init_calls == 1, (
                f"LIVE entry after a PURE_SIM run must still initialize "
                f"devices; observed {driver.init_calls} dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_each_device_world_is_initialized_once(self) -> None:
        """Init keys on the resolved per-device world, mirroring deck config.

        A pinned-sim device resolves PURE_SIM under every submission mode,
        so it is never initialized; a wire-mode device is initialized once
        per mode because the device bridge swaps its backend per stamped mode.
        """
        pinned_driver = CountingInitDriver("pinned")
        pinned = UniversalMockDevice(
            "pinned", sim_override=WorkflowRunMode.PURE_SIM, driver=pinned_driver,
        )
        wired_driver = CountingInitDriver("wired")
        wired = UniversalMockDevice("wired", driver=wired_driver)
        runtime = await _build_lazy_runtime(
            {"pinned": pinned, "wired": wired},
            teach=("pinned", "wired", "pad1"),
        )
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.DEVICE_SIM):
                await runtime.system.ensure_runtime_initialized()
            assert pinned_driver.init_calls == 0, (
                "A PURE_SIM-resolved world needs no bring-up."
            )
            assert wired_driver.init_calls == 1
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
            assert pinned_driver.init_calls == 0, (
                "The pinned device's world did not change; a second "
                "submission mode must not re-initialize it."
            )
            assert wired_driver.init_calls == 2, (
                "DEVICE_SIM and LIVE are different wire backends; each "
                "needs its own init."
            )
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


def _build_recorded_lh(
    sim_override: WorkflowRunMode | None = None,
) -> tuple[LiquidHandler, RecordingLiquidHandlerDriver, RecordingLiquidHandlerDriver]:
    """A LiquidHandler whose live and sim drivers are DISTINCT recorders,
    so a test can see which world a dispatch landed in."""
    live = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    sim = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    stores = InMemoryRuntimeStoreFactory()
    with use_device_factory(PairDriverFactory(live, sim)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts(
                "lh", seed={"default": LH_DECK_CONFIG},
            ),
            deck_layout="default",
            sim_override=sim_override,
        )
    return lh, live, sim


def _configure_calls(driver: RecordingLiquidHandlerDriver) -> int:
    return sum(1 for call in driver.calls if call.method == "configure_deck")


class TestDeckConfiguredPerWorld:
    """`LiquidHandler.deck_world_layout` lays out each resolved world.

    The sim and live drivers are separate worlds: a configure_deck pushed
    to the sim driver under PURE_SIM leaves the live instrument bare. The
    done-gate must therefore key on the resolved per-device mode, not a
    process-wide boolean.
    """

    async def test_each_world_gets_its_own_configure(self) -> None:
        lh, live, sim = _build_recorded_lh()

        with seeded(WorkflowRunMode.PURE_SIM):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, True)
        assert _configure_calls(sim) == 1
        assert _configure_calls(live) == 0, (
            "PURE_SIM configure must not touch the live driver."
        )

        with seeded(WorkflowRunMode.LIVE):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, True)
        assert _configure_calls(live) == 1, (
            "The LIVE world was never configured; the PURE_SIM configure "
            "must not satisfy it."
        )

        with seeded(WorkflowRunMode.LIVE):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, False)
        assert _configure_calls(live) == 1, (
            "A second LIVE call must observe the LIVE world already "
            "configured."
        )
        assert _configure_calls(sim) == 1

    async def test_override_pins_the_configured_world(self) -> None:
        """sim_override=PURE_SIM: both submission modes resolve to the sim
        world, so the second call is a no-op on an already-configured world."""
        lh, live, sim = _build_recorded_lh(sim_override=WorkflowRunMode.PURE_SIM)

        with seeded(WorkflowRunMode.PURE_SIM):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, True)
        with seeded(WorkflowRunMode.LIVE):
            assert await lh.deck_world_layout() == (LH_DECK_CONFIG, False)
        assert _configure_calls(sim) == 1
        assert _configure_calls(live) == 0

    async def test_concurrent_callers_configure_a_world_once(self) -> None:
        """The walk and a facade-triggered reconcile can race into the
        configure; the device-level lock must collapse them to one dispatch."""
        lh, live, _ = _build_recorded_lh()
        with seeded(WorkflowRunMode.LIVE):
            results = await asyncio.gather(
                lh.deck_world_layout(),
                lh.deck_world_layout(),
                lh.deck_world_layout(),
            )
        assert _configure_calls(live) == 1, (
            f"Concurrent callers dispatched {_configure_calls(live)} "
            f"configure_deck calls; the lock must allow exactly one."
        )
        assert sum(1 for _layout, fresh in results if fresh) == 1, (
            "Exactly one caller must report a fresh configure."
        )

    async def test_reset_in_one_world_keeps_the_other_worlds_identity_guard(
        self,
    ) -> None:
        """`reset_labware_state` under one mode must not disarm the other
        world's DeckLabwareIdentityError guard: the instance map is per
        world, like the deck it mirrors."""
        lh, live, sim = _build_recorded_lh()
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
            with seeded(WorkflowRunMode.PURE_SIM):
                await lh.deck_world_layout()
                await lh.reset_labware_state()
            impostor = PlateInstance(
                SimPlate(first.name), template_name=first.template_name, labware_type=first.labware_type,
            )
            assert impostor.id != first.id and impostor.name == first.name
            with seeded(WorkflowRunMode.LIVE):
                with pytest.raises(DeckLabwareIdentityError):
                    await lh._do_notify_placed(
                        impostor, EXTERNAL_MOVER, target="carrier-7-1",
                    )
        finally:
            await runtime.shutdown()


class TestLazyInitWalkReconcilesPerWorld:
    """The lazy-init walk reconciles a deck world once, when it is fresh.

    A second submission mode that resolves an LH to an already-configured
    world (sim_override=PURE_SIM under a LIVE submission) must not
    re-reconcile it: reconcile is a clear+rebuild that resets occupant
    volume/tip state, so re-running it on a world kept in sync since its
    first configure would wipe accumulated state. A FAILED reconcile,
    though, stays owed: the next entry must retry it even though the
    configure itself is not repeated.
    """

    async def test_pinned_sim_lh_reconciled_once_across_modes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lh, live, sim = _build_recorded_lh(sim_override=WorkflowRunMode.PURE_SIM)
        runtime = await _build_lazy_runtime({"lh": lh})
        system = runtime.system
        reconcile = AsyncMock()
        monkeypatch.setattr(system, "reconcile_lh_deck_occupancy", reconcile)
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.PURE_SIM):
                await system.ensure_runtime_initialized()
            assert _configure_calls(sim) == 1
            assert reconcile.await_count == 1

            with seeded(WorkflowRunMode.LIVE):
                await system.ensure_runtime_initialized()
            assert _configure_calls(sim) == 1, (
                "The pinned-sim LH resolves to the already-configured sim "
                "world; the LIVE walk must not reconfigure it."
            )
            assert _configure_calls(live) == 0
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
        lh, live, sim = _build_recorded_lh()
        runtime = await _build_lazy_runtime({"lh": lh})
        system = runtime.system
        reconcile = AsyncMock(side_effect=[RuntimeError("wire dropped"), None])
        monkeypatch.setattr(system, "reconcile_lh_deck_occupancy", reconcile)
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                with pytest.raises(RuntimeError, match="wire dropped"):
                    await system.ensure_runtime_initialized()
                await system.ensure_runtime_initialized()
            assert _configure_calls(live) == 1, (
                "The retry must not re-dispatch configure_deck to a world "
                "that already has its carrier skeleton."
            )
            assert reconcile.await_count == 2, (
                f"The failed reconcile must be retried; observed "
                f"{reconcile.await_count} attempts."
            )
        finally:
            await runtime.shutdown()
