"""
Test helper utilities for Orca tests.

Provides common utilities for building test systems, creating test data,
and waiting for async operations to complete.
"""
import asyncio
import re
import inspect
import traceback
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import List, Dict

import pytest

from cheshire_drivers import (
    AccessConfig,
    Teachpoint,
    CartesianCoordinates,
    RecordingLiquidHandlerDriver,
    SimDriver,
    SimTranslatorDriver,
)
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.execution import ExecutionPhase
from orca.runtime.status_models import ExecutionStatus, ThreadSnapshot
from orca.runtime.submission import Submission
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.interfaces import ITeachpointStore
from orca.runtime.run_modes import WorkflowRunMode, current_run_mode
from orca.runtime.teachpoint_service import seeded_teachpoint_service
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import ILabwareLocationService
from orca.resource_models.labware_placement import LabwarePlacer
from orca.resource_models.location import Location
from orca.resource_models.devices import Device
from orca.resource_models.plate_pad import PlatePad
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from orca.resource_models.labware import (
    LabwareInstance,
    PlateInstance,
    PlateTemplate,
    TipRackInstance,
    TipRackTemplate,
    TroughInstance,
    TroughTemplate,
)
from orca.runtime.labware_catalog_protocol import ILabwareCatalog
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from tests.mock import UniversalMockDevice


def create_test_teachpoints(names: List[str]) -> List[Teachpoint]:
    """
    Create simple teachpoints for testing.

    Args:
        names: List of teachpoint names

    Returns:
        List of Teachpoint objects with Cartesian coordinates (required for access_type)
    """
    default_vertical = AccessConfig(name="default_vertical", access_type="vertical")
    teachpoints = []
    for i, name in enumerate(names):
        # Use Cartesian coordinates (access_type requires Cartesian for pick/place)
        coords = CartesianCoordinates(
            x=float(i * 50 + 100),
            y=0.0,
            z=50.0,
            yaw=180.0,
            pitch=90.0,
            roll=0.0
        )
        teachpoint = Teachpoint(
            position_id=name,
            coordinates=coords,
            orientation="right",
            access=default_vertical,
        )
        teachpoints.append(teachpoint)
    return teachpoints


def create_test_transporter(
    name: str,
    position_ids: List[str],
    sim_override: WorkflowRunMode | None = None,
    single_carriage: bool = False,
) -> Transporter:
    """
    Create a test transporter with simple teachpoints.

    Args:
        name: Transporter name
        position_ids: Names of locations this transporter can reach
        sim_override: Per-device topology sim_override declared at construction
            time. Mirror of Device.sim_override; surfaced on the topology card.
        single_carriage: Bind a ``SimTranslatorDriver`` so the transporter
            declares one-carriage semantics the way production does: the
            DRIVER owns the physical truth (TransporterBase.single_carriage).

    Returns:
        Configured Transporter instance
    """
    from orca.runtime.device_factory_context import use_device_factory

    teachpoints = create_test_teachpoints(position_ids)
    store = seeded_teachpoint_service(teachpoints)
    if single_carriage:
        with use_device_factory(_SingleDriverFactory(SimTranslatorDriver(name))):
            return Transporter(
                name, teachpoint_store=store, sim_override=sim_override,
            )
    return Transporter(
        name, teachpoint_store=store,
        sim_override=sim_override,
    )


class FailOnPlaceTransporter(Transporter):
    """Transporter that succeeds on pick() but raises on place().

    Simulates a physical failure where the arm picks the plate but
    crashes before placing it. The plate is "in the gripper".
    """

    def __init__(self, name: str, position_ids: List[str]) -> None:
        teachpoints = create_test_teachpoints(position_ids)
        store = seeded_teachpoint_service(teachpoints)
        super().__init__(name, teachpoint_store=store)
        self.should_fail_place = True
        self.place_call_count = 0
        self.place_targets: List[str] = []

    async def place(self, location: Location) -> None:
        self.place_call_count += 1
        self.place_targets.append(location.position_id)
        if self.should_fail_place:
            raise RuntimeError("Simulated place failure: arm jammed")
        await super().place(location)


class _SingleDriverFactory:
    """Factory that returns the same driver for both slots.

    Used by tests that need to inject a specific Sim* / mock driver instance
    into a Device built via the no-driver SDK ctor. Bind via
    ``use_device_factory(...)`` for the duration of the topology build.
    """

    def __init__(self, driver: DriverPairElement) -> None:
        self._driver = driver

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        return self._driver, self._driver


def named_for_template(name: str, template: str) -> bool:
    """True when a driver-world name IS an instance of ``template``.

    Exact shape match (``<template>-<8 hex>``), not a prefix test: driver deck
    listings include PLR child resources (``reservoir-57ca11ab_well_A1``) that a
    prefix test would miscount as instances."""
    return re.fullmatch(re.escape(template) + r"-[0-9a-f]{8}", name) is not None


def template_of(name: str) -> str:
    """Recover the template prefix from an instance name."""
    return name.rsplit("-", 1)[0]


def assert_translator_carriage_pairs(system_map: SystemMap) -> None:
    """Drift guard shared by the SMC examples: both bridge translators must
    register one-carriage sibling groups. A factory change that silently hands
    them the generic arm sim re-arms the N=6 head-on livelock without failing
    any unit test."""
    for start, end in (("translator_1_start", "translator_1_end"),
                       ("translator_2_start", "translator_2_end")):
        siblings = [loc.position_id for loc in system_map.exclusion_siblings_of(start)]
        assert siblings == [end], (
            f"{start} has no carriage sibling group; the translator driver "
            f"declaration was lost (got {siblings})"
        )


def make_transporter_with_driver(
    name: str,
    driver: DriverPairElement,
    teachpoint_store: ITeachpointStore,
) -> Transporter:
    """Build a Transporter wired with a specific test driver instance.

    Tests subclass SimTransporterDriver to record dispatches or raise on
    demand; this helper threads that instance through the no-driver Device
    ctor via the factory contextvar.
    """
    from orca.runtime.device_factory_context import use_device_factory
    with use_device_factory(_SingleDriverFactory(driver)):
        return Transporter(
            name, teachpoint_store=teachpoint_store,
        )


def create_test_device(
    name: str,
    device_type: str = "device",
    sim_override: WorkflowRunMode | None = None,
    site_names: list[str] | None = None,
) -> UniversalMockDevice:
    """
    Create a test device that supports all action interfaces.

    Args:
        name: Device name
        device_type: Type of device for simulation (deprecated, kept for compatibility)
        sim_override: Per-device topology sim_override declared at construction
            time. Surfaced on `TopologyCard.topology_sim_override` and consulted
            by the mode resolver.

    Returns:
        UniversalMockDevice instance supporting all action types
    """
    del device_type
    return UniversalMockDevice(name, sim_override=sim_override, site_names=site_names)


def create_test_plate_template(name: str = "test_plate") -> PlateTemplate:
    """
    Create a test plate template.

    Args:
        name: Plate template name

    Returns:
        PlateTemplate instance
    """
    # SimPlateTemplate bypasses the catalog -- tests calling
    # `template.create_instance()` outside SdkToSystemBuilder do not bind
    # one, and the staging-bridge / template-backref tests that consume
    # this helper do not depend on real PLR geometry.
    from orca.runtime.sim_labware import SimPlateTemplate
    return SimPlateTemplate(name)


async def create_test_labware_instance(name: str = "test_plate") -> PlateInstance:
    """Create a test labware instance via the sim path (no catalog needed)."""
    from orca.runtime.sim_labware import SimPlateTemplate
    return await SimPlateTemplate(name).create_instance()


# --- Factory-based template subclasses for tests that mock the underlying
# labware factory directly. These bypass the catalog (bind_catalog is a
# no-op) and use the supplied callable to build the instance, mirroring
# how SimPlateTemplate sidesteps the catalog. Used by test_initial_state,
# test_can_continue_layers, test_plate_instance_has_lid, test_batch_mode_keying,
# and test_shared_across_groups_keying -- tests that exercise template
# machinery (initial state, capacity layers, group keying) with mock
# plates/troughs/racks instead of real PLR labware.


class FactoryPlateTemplate(PlateTemplate):
    """PlateTemplate that takes an explicit factory callable. Test-only."""

    def __init__(
        self,
        name: str,
        labware_factory,
        with_lid: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(name, labware_type="_test_factory_plate", with_lid=with_lid, **kwargs)
        self._test_factory = labware_factory

    async def bind_catalog(self, catalog: ILabwareCatalog) -> None:
        return

    async def _build_instance(self, instance_id: str, instance_name: str) -> PlateInstance:
        try:
            plate = self._test_factory(instance_name, self._with_lid if self._with_lid else None)
        except TypeError:
            plate = self._test_factory(instance_name)
        instance = PlateInstance(plate, template_name=self.name, labware_type=self.labware_type, instance_id=instance_id)
        instance._template = self
        return instance


class FactoryTroughTemplate(TroughTemplate):
    """TroughTemplate that takes an explicit factory callable. Test-only."""

    def __init__(self, name: str, labware_factory, **kwargs) -> None:
        super().__init__(name, labware_type="_test_factory_trough", **kwargs)
        self._test_factory = labware_factory

    async def bind_catalog(self, catalog: ILabwareCatalog) -> None:
        return

    async def _build_instance(self, instance_id: str, instance_name: str) -> TroughInstance:
        instance = TroughInstance(
            self._test_factory(instance_name),
            template_name=self.name, labware_type=self.labware_type, instance_id=instance_id,
        )
        instance._template = self
        return instance


class FactoryTipRackTemplate(TipRackTemplate):
    """TipRackTemplate that takes an explicit factory callable. Test-only."""

    def __init__(
        self,
        name: str,
        labware_factory,
        with_tips: bool,
        **kwargs,
    ) -> None:
        super().__init__(name, labware_type="_test_factory_tip_rack", with_tips=with_tips, **kwargs)
        self._test_factory = labware_factory

    async def bind_catalog(self, catalog: ILabwareCatalog) -> None:
        return

    async def _build_instance(self, instance_id: str, instance_name: str) -> TipRackInstance:
        instance = TipRackInstance(
            self._test_factory(instance_name, self._with_tips),
            template_name=self.name, labware_type=self.labware_type, instance_id=instance_id,
        )
        instance._template = self
        return instance


async def create_simple_system_map(
    transporters: List[Transporter],
    devices: Dict[str, Device],
    parking_pads: Dict[str, PlatePad] | None = None
) -> tuple[ResourceRegistry, SystemMap]:
    """
    Create a simple system map for testing.

    Args:
        transporters: List of transporter resources
        devices: Dict of device_name -> Device
        parking_pads: Optional dict of pad_name -> PlatePad

    Returns:
        Tuple of (ResourceRegistry, SystemMap)
    """
    registry = ResourceRegistry()
    for transporter in transporters:
        registry.add_resource(transporter)
    for device in devices.values():
        registry.add_resource(device)

    # Flat-model build order: pads and device sites BEFORE
    # transporter edges; unknown teachpoints fail loud.
    system_map = SystemMap(registry)
    for pad_name, pad in (parking_pads or {}).items():
        await system_map.add_location(Location(pad_name, pad))
    for name, device in devices.items():
        await _assign_device(system_map, name, device)
    await system_map.initialize_transporters()

    return registry, system_map


async def _assign_device(system_map: SystemMap, name: str, device: Device) -> None:
    """Register a device the way build_system does under the flat model:
    a '<name>/slot' site node (bridge-backed) plus the off-graph mutex.

    Must run BEFORE `initialize_transporters` so a teachpoint naming the
    device resolves to its site (unknown teachpoints fail loud).
    """
    from orca.sdk.build import add_device_sites
    await add_device_sites(system_map, name, device)


async def wire_system_map(
    system_map: SystemMap,
    devices: Mapping[str, Device] | None = None,
    pads: Iterable[str] = (),
) -> None:
    """One-call fixture wiring in the flat-model build order: pads, then
    device sites, then transporter edges (which need both to exist)."""
    for pad in pads:
        await system_map.add_location(Location(pad))
    for name, device in (devices or {}).items():
        await _assign_device(system_map, name, device)
    await system_map.initialize_transporters()


@contextmanager
def seeded(mode: WorkflowRunMode) -> Iterator[None]:
    """Seed `current_run_mode` for the block, always resetting the token."""
    token = current_run_mode.set(mode)
    try:
        yield
    finally:
        current_run_mode.reset(token)


class PairDriverFactory:
    """Device factory that hands out a fixed (live, sim) driver pair, so a
    test can observe which world a dispatch landed in."""

    def __init__(self, live: DriverPairElement, sim: DriverPairElement) -> None:
        self._live = live
        self._sim = sim
        self._fallback = SimDeviceFactory()

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        if device_type == "liquid_handler":
            return self._live, self._sim
        return self._fallback.build_drivers(
            device_type, name, deck_modeling=deck_modeling,
        )


class _ConditionSink:
    """Wakes an asyncio.Event when a runtime event makes ``predicate`` hold.

    The engine emits a RuntimeEvent per status transition; this re-checks the
    predicate on each one, so a wait tracks real transitions instead of a sleep.
    Incident events can be recorded off-loop, so the re-check is marshalled onto
    the loop before the Event is touched.
    """

    def __init__(self, predicate: Callable[[], bool]) -> None:
        self._predicate = predicate
        self._loop = asyncio.get_running_loop()
        self.satisfied = asyncio.Event()

    def on_event(self, event: RuntimeEvent) -> None:
        self._loop.call_soon_threadsafe(self._recheck)

    def _recheck(self) -> None:
        if not self.satisfied.is_set() and self._predicate():
            self.satisfied.set()


async def wait_for_runtime_condition(
    runtime: SystemRuntime,
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    message: str | None = None,
) -> None:
    """Await until ``predicate()`` holds, woken by runtime events, never polling.

    Subscribes to the SystemEventBus and re-checks the predicate on each emitted
    RuntimeEvent, so the wait tracks the engine's own transitions (thread
    paused/terminal, execution lifecycle, incidents). ``timeout`` is only a hang
    ceiling. For predicates the runtime does not drive via events (fakes, queues,
    standalone services) use ``wait_until`` -- there is no event to wake on.
    """
    sink = _ConditionSink(predicate)
    runtime.register_sink(sink)
    try:
        if predicate():
            return
        await asyncio.wait_for(sink.satisfied.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(message or f"condition not met within {timeout}s")
    finally:
        runtime.unregister_sink(sink)


async def wait_for_paused_threads(
    runtime: SystemRuntime,
    execution_id: str,
    *,
    count: int = 1,
    timeout: float = 10.0,
) -> List[ThreadSnapshot]:
    """Await until at least ``count`` threads in the execution are PAUSED.

    Event-driven replacement for the per-file ``_wait_for_paused`` poll loops:
    the engine emits THREAD.{id}.PAUSED, so this wakes on that transition instead
    of sampling get_paused_threads() on a timer. Returns the paused snapshots.
    """
    await wait_for_runtime_condition(
        runtime,
        lambda: len(runtime.get_paused_threads(execution_id)) >= count,
        timeout=timeout,
        message=f"{count} thread(s) did not reach PAUSED within {timeout}s",
    )
    return runtime.get_paused_threads(execution_id)


async def wait_for_paused_thread(
    runtime: SystemRuntime, execution_id: str, *, timeout: float = 10.0,
) -> ThreadSnapshot:
    """Await until a thread in the execution is PAUSED; return the first one."""
    return (await wait_for_paused_threads(runtime, execution_id, timeout=timeout))[0]


async def run_to_quiescence(
    runtime: SystemRuntime, execution_id: str, *, timeout: float = 40.0,
) -> Dict[str, str]:
    """Await until every thread in the execution reaches a terminal state.

    Event-driven: wakes on each THREAD.* transition and checks the terminal set
    instead of sampling list_threads() on a timer. On a genuine hang it returns
    whatever statuses were reached when the ceiling elapsed, matching the prior
    poll helper's non-raising contract. ``timeout`` is that hang ceiling.
    """
    terminal = {"COMPLETED", "ABORTED", "STOPPED", "FAILED"}

    def _all_terminal() -> bool:
        statuses = [t.status for t in runtime.list_threads(execution_id)]
        return bool(statuses) and all(s in terminal for s in statuses)

    try:
        await wait_for_runtime_condition(runtime, _all_terminal, timeout=timeout)
    except TimeoutError:
        pass
    return {t.name: t.status for t in runtime.list_threads(execution_id)}


async def execution_outcome(
    runtime: SystemRuntime, submission: Submission, *, timeout: float,
) -> ExecutionStatus:
    """Wait for the submission's execution to reach a TERMINAL phase, or fail
    loudly with a thread dump.

    On timeout the failure names every thread and its state plus every
    pending asyncio task's top frames, so a wedged run pins WHERE it lost
    its wakeup (the lost-wakeup class only reproduces on starved CI cores).
    Every test that bounds this wait goes through here.
    """
    def _thread_dump() -> str:
        threads = ", ".join(
            f"{t.name}[{t.status}]"
            for t in runtime.list_threads(submission.execution_id)
        )
        return threads or "none"

    def _task_stacks() -> str:
        return "\n".join(
            f"--- {task.get_name()}\n"
            + "".join(traceback.format_stack(frame, limit=3))
            for task in asyncio.all_tasks()
            for frame in task.get_stack(limit=3)[-1:]
        )

    try:
        status = await asyncio.wait_for(
            runtime.wait_for_execution(submission), timeout=timeout,
        )
    # asyncio.TimeoutError: NOT the builtin on CI's 3.10; alias of it on 3.11+.
    except asyncio.TimeoutError:
        pytest.fail(
            f"execution not terminal after the {timeout}s budget; "
            f"threads: {_thread_dump()}\n"
            f"pending task stacks:\n{_task_stacks()}"
        )
    if status.status in (
        ExecutionPhase.COMPLETED, ExecutionPhase.FAILED, ExecutionPhase.ABORTED,
    ):
        return status
    pytest.fail(
        f"execution finished in non-terminal phase {status.status}; "
        f"threads: {_thread_dump()}"
    )


async def wait_until(
    predicate: Callable[[], bool | Awaitable[bool]],
    *,
    timeout: float = 5.0,
    message: str | None = None,
) -> None:
    """Drive the event loop until ``predicate()`` (sync or async) is truthy.

    Condition-driven, not time-driven, so it never flakes under load; the
    timeout is only a safety ceiling so a genuine hang fails loudly instead of
    spinning forever. Use in place of ``asyncio.sleep(<guess>)``-then-assert.
    """
    async def _run() -> None:
        while True:
            result = predicate()
            if inspect.isawaitable(result):
                result = await result
            if result:
                return
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(message or f"condition not met within {timeout}s")


class RecordingLhDeckFactory:
    """Device factory that returns a recording liquid-handler driver for the
    liquid_handler device and the default sim drivers for everything else."""

    def __init__(self, lh: RecordingLiquidHandlerDriver) -> None:
        self._lh = lh
        self._fallback = SimDeviceFactory()

    def build_drivers(self, device_type: str, name: str, *, deck_modeling: bool = False):
        if device_type == "liquid_handler":
            return self._lh, self._lh
        return self._fallback.build_drivers(device_type, name)


def bind_ledger(
    instance: LabwareInstance, history: OpsHistory,
) -> LabwareContentsLedger:
    """The ledger a labware built straight from a template would otherwise
    never be told about. Binding only; `enter_record` is what birth calls."""
    ledger = LabwareContentsLedger(history)
    instance.bind_contents(ledger)
    return ledger


async def _no_projection(
    labware: LabwareInstance, *locations: Location,
) -> None:
    """No liquid-handler deck to push to in a unit test."""
    return None


def make_labware_placer(
    location_service: ILabwareLocationService,
) -> LabwarePlacer:
    """The placement chokepoint without the deck projection."""
    return LabwarePlacer(location_service, _no_projection)


class _NoSourceHold:
    """The source-slot holder for tests that are not about that hold.

    A mover whose pick lifts the plate out never asks for one, so most tests
    never reach this; it keeps the ones with a one-call mover explicit about
    holding nothing rather than picking up a mock reservation."""

    async def hold_the_source(
        self, thread_id: str, labware: LabwareInstance, source: Location,
    ) -> None:
        return None


def no_source_hold() -> _NoSourceHold:
    return _NoSourceHold()
