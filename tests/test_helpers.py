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
)
from orca.resource_models.resources import IModeAware
from orca.resource_models.transporter import Transporter
from orca.resource_models.translator import Translator
from orca.driver_management.device_factory_protocol import DeviceDriver
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.execution import ExecutionPhase
from orca.runtime.status_models import ExecutionStatus, ThreadSnapshot
from orca.workflow_models.submission import Submission
from orca.runtime.system_runtime import SystemRuntime
from orca.modes.device_modes import DeviceMode
from tests.device_modes import set_to
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import ILabwareLocationService
from orca.resource_models.labware_service_interface import ILabwareService
from orca.resource_models.labware_placement import LabwarePlacer
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.command_effects import CommandStatement, LabwareMotion, SystemCommandPipeline
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.vendor_protocol_head import VendorProtocolHeadCheck
from orca.devices.device_interfaces import ILiquidHandler
from orca.state.mounted import MountedTips
from orca.state.provenance import Provenance
from orca.gateway.controller.command_pipeline import DeviceCommandPipeline
from orca.resource_models.location import Location
from orca.resource_models.devices import Device
from orca.resource_models.plate_pad import PlatePad
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from orca.state.records import OperationRecord
from orca.resource_models.tracking_interpreter import DefaultInterpreter, IOperationInterpreter
from orca.workflow_models.actions.device_call_dispatcher import CommandRecorder
from orca.resource_models.labware import (
    LabwareInstance,
    PlateInstance,
    PlateTemplate,
    TipRackInstance,
    TipRackTemplate,
    TroughInstance,
    TroughTemplate,
)
from orca.resource_models.labware_catalog_protocol import ILabwareCatalog
from orca.resource_models.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from tests.mock import UniversalMockDevice, outside_any_system
from tests.setup_package import package_stores, with_teachpoints, write_teachpoints
from tests.real_device_path import the_real_device_path
from tests.memory_activity import memory_labware_service, memory_unsaved_positions


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
    setting: DeviceMode | None = None,
    single_carriage: bool = False,
) -> Transporter:
    """
    Create a test transporter with simple teachpoints.

    Args:
        name: Transporter name
        position_ids: Names of locations this transporter can reach
        setting: The transporter's sim or live setting; sim when omitted.
        single_carriage: Build a ``Translator``, whose driver declares one carriage.

    Returns:
        Configured Transporter instance
    """
    teachpoints = create_test_teachpoints(position_ids)
    store = write_teachpoints(package_stores(), name, teachpoints)
    transporter = with_teachpoints(Translator(name) if single_carriage else Transporter(name), store)
    if setting is not None:
        set_to(transporter, name, setting)
    return outside_any_system(transporter)


class FailOnPlaceTransporter(Transporter):
    """Transporter that succeeds on pick() but raises on place().

    Simulates a physical failure where the arm picks the plate but
    crashes before placing it. The plate is "in the gripper".
    """

    def __init__(self, name: str, position_ids: List[str]) -> None:
        teachpoints = create_test_teachpoints(position_ids)
        store = write_teachpoints(package_stores(), name, teachpoints)
        super().__init__(name)
        self.bind_teachpoints(store)
        outside_any_system(self)
        self.should_fail_place = True
        self.place_call_count = 0
        self.place_targets: List[str] = []

    async def place(self, location: Location) -> None:
        self.place_call_count += 1
        self.place_targets.append(location.position_id)
        if self.should_fail_place:
            raise RuntimeError("Simulated place failure: arm jammed")
        await super().place(location)


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
    driver: DeviceDriver,
) -> Transporter:
    """A Transporter whose commands the in-process orca-client runs on `driver`."""
    the_real_device_path().simulate_with(name, driver)
    return outside_any_system(Transporter(name))


def create_test_device(
    name: str,
    device_type: str = "device",
    setting: DeviceMode | None = None,
    site_names: list[str] | None = None,
) -> UniversalMockDevice:
    """
    Create a test device that supports all action interfaces.

    Args:
        name: Device name
        device_type: Type of device for simulation (deprecated, kept for compatibility)
        setting: The device's sim or live setting; sim when omitted.

    Returns:
        UniversalMockDevice instance supporting all action types
    """
    del device_type
    device = UniversalMockDevice(name, site_names=site_names)
    if setting is not None:
        set_to(device, name, setting)
    return device


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
    from orca.resource_models.sim_labware import SimPlateTemplate
    return SimPlateTemplate(name)


async def create_test_labware_instance(name: str = "test_plate") -> PlateInstance:
    """Create a test labware instance via the sim path (no catalog needed)."""
    from orca.resource_models.sim_labware import SimPlateTemplate
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
        **kwargs,
    ) -> None:
        super().__init__(name, labware_type="_test_factory_plate", **kwargs)
        self._test_factory = labware_factory

    async def bind_catalog(self, catalog: ILabwareCatalog) -> None:
        return

    async def _build_instance(self, instance_id: str, instance_name: str) -> PlateInstance:
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
    await the_real_device_path().connect_built()

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
    await the_real_device_path().connect_built()


@contextmanager
def seeded(mode: WorkflowRunMode) -> Iterator[None]:
    """Seed `current_run_mode` for the block, always resetting the token."""
    token = current_run_mode.set(mode)
    try:
        yield
    finally:
        current_run_mode.reset(token)


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
        ExecutionPhase.COMPLETED, ExecutionPhase.PARTIAL_COMPLETED, ExecutionPhase.FAILED, ExecutionPhase.ABORTED,
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


async def _no_rebuild(device_location: Location) -> None:
    return None


async def _no_staling() -> None:
    return None


def no_forgetting(labware: LabwareInstance) -> None:
    """For a placer with no system behind it: nothing lists the labware, so nothing forgets it."""


def make_labware_placer(
    location_service: ILabwareLocationService, labware_service: ILabwareService | None = None,
) -> LabwarePlacer:
    """The placement chokepoint without the deck projection, saving to ``labware_service`` or a fresh one."""
    return LabwarePlacer(
        location_service, labware_service or memory_labware_service(), memory_unsaved_positions(), _no_projection, _no_staling, _no_staling,
        _no_rebuild, no_forgetting,
    )


async def _save_by_name_nowhere(device_name: str, command: str, args_repr: str) -> None:
    return None


def _no_conflict_report(device_name: str, motion: LabwareMotion, occupied: SlotOccupiedError) -> None:
    raise occupied


class _FindsNothing:
    """A statement lookup for a pipeline with no system behind it."""

    def labware_with_id(self, labware_id: str) -> LabwareInstance | None:
        return None

    def location_at(self, position_id: str) -> Location | None:
        return None

    def mover_named(self, name: str) -> IPlateMover | None:
        return None


def _no_labware(device_name: str) -> list[LabwareInstance]:
    return []


def make_command_pipeline(location_service: ILabwareLocationService) -> SystemCommandPipeline:
    """The system pipeline over a placer without the deck projection, saving engine commands nowhere."""
    return command_pipeline_over(make_labware_placer(location_service))


class _NoHeads:
    """A head record for a pipeline with no liquid handler behind it."""

    async def of(self, device_name: str) -> MountedTips:
        return MountedTips({}, Provenance.UNKNOWN)


def _no_liquid_handler(device_name: str) -> bool:
    return False


def command_pipeline_over(placer: LabwarePlacer) -> SystemCommandPipeline:
    return SystemCommandPipeline(
        placer, _save_by_name_nowhere, _no_conflict_report, _no_labware, _FindsNothing(),
        VendorProtocolHeadCheck(_NoHeads(), _no_liquid_handler),
    )


def engine_pipeline() -> DeviceCommandPipeline[CommandStatement]:
    """A pipeline with no labware effects, for a device outside any system."""
    return DeviceCommandPipeline(())


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


def kept_engine_commands(resource: IModeAware) -> list[str]:
    """A device outside any system, whose engine pushes are kept in the returned list instead of a history."""
    kept: list[str] = []

    async def keep(command: str) -> None:
        kept.append(command)

    resource.engine_commands.save_with(keep, engine_pipeline())
    return kept


def command_recorder(
    interpreter: IOperationInterpreter | None = None,
    into: list[OperationRecord] | None = None,
    device_name: str = "dev",
    action_id: str = "a1",
    thread_id: str = "t1",
) -> CommandRecorder:
    """A recorder that saves into a list, for tests of the dispatcher alone."""
    saved = into if into is not None else []

    async def save(operations: list[OperationRecord]) -> None:
        saved.extend(operations)

    return CommandRecorder(interpreter or DefaultInterpreter(), save, device_name, action_id, thread_id)


async def let_the_loop_run(turns: int = 50) -> None:
    """Give every ready task this many scheduler turns, to show something did NOT happen without a wall-clock guess."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def state_every_liquid_handler_head_empty(runtime: SystemRuntime) -> None:
    """What an operator does before a vendor protocol: state that each liquid handler's head carries nothing."""
    for resource in runtime.system.resources:
        if isinstance(resource, ILiquidHandler):
            await runtime.system.mounted_tips.assert_mounted(resource.name, {})
