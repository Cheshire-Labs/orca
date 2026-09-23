"""Operator clear tools on LabwareFacade.

`discharge_labware`, `clear_all_labware`, and `clear_submission_labware`
let the operator recover when the pre-submit check refused a submission
because a start_location was occupied.

All three remove labware from three stores:
- `system.labwares` (in-memory engine registry)
- `ILabwareStore` (identity / barcode index)
- `Location.labware` slot if currently held

Reuse-bound labware (thread template declared end_leave_in_place) is
SKIPPED by clear_submission_labware so deck-resident reagents survive
selective clears. clear_all_labware ignores the skip; it's the panic
button.
"""

import asyncio
from collections.abc import Iterator, Sequence
from unittest.mock import MagicMock

import pytest

from cheshire_drivers.liquid_handler_models import (
    LabwareStateResponse, ResetDeckLabwareRequest,
)

from cheshire_drivers.interfaces import ITransporterDriver
from cheshire_drivers.transporter_models import ResetWorldRequest
from orca.devices.devices import LiquidHandler
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.resources import ILabwareStateHolder
from orca.runtime.deck_layout_service import seeded_deck_layout_service
from orca.runtime.execution import Execution, ExecutionPhase
from orca.runtime.facades.labware import LabwareFacade
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.runtime_interface import (
    ActiveExecutionRefusedError, LabwareNotFoundError,
)
from orca.runtime.system_runtime import SystemRuntime
from orca.system.system_interface import ISystem
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.test_helpers import create_test_device, create_test_transporter
from tests.test_system_runtime import _build_simple_system


class _StubExecutionIterator:
    """Test double for IExecutionIterator.

    Returns the executions you hand it; lets a test inject an active
    execution into LabwareFacade without spinning up a real workflow.
    """

    def __init__(self, executions: list[Execution]) -> None:
        self._executions = executions

    def iter_executions(self) -> Iterator[Execution]:
        return iter(self._executions)


def _make_pending_task() -> asyncio.Task[None]:
    """Build a Task that has not yet completed (task.done() is False)."""
    loop = asyncio.get_event_loop()

    async def _never_done() -> None:
        await asyncio.sleep(3600)

    return loop.create_task(_never_done())


def _make_active_execution(
    system, submission_id: str = "sub-1",
    threads_view: Sequence[object] | None = None,
) -> Execution:
    """Construct an Execution whose `task.done()` is False and whose
    `executing_workflow.threads` returns the supplied stub threads."""
    task = _make_pending_task()
    execution = Execution(
        id="ex-1",
        workflow_name="wf",
        workflow=MagicMock(),
        system=system,
        task=task,
        phase=ExecutionPhase.ACCEPTING,
    )
    if threads_view is not None:
        ew = MagicMock()
        ew.threads = threads_view
        execution.executing_workflow = ew
    return execution


def _make_thread_stub(
    submission_id: str, labware: LabwareInstance | None,
    *, end_leave_in_place: bool = False,
    thread_template: object | None = None,
    status: LabwareThreadStatus | None = None,
):
    """Stub thread shape consumed by LabwareFacade._has_active_thread_for
    and clear_submission_labware (reads `thread.thread_instance`)."""
    ti = MagicMock()
    ti.submission_id = submission_id
    ti.labware = labware
    if thread_template is None:
        tt = MagicMock()
        tt.end_leave_in_place = end_leave_in_place
        ti.thread_template = tt
    else:
        ti.thread_template = thread_template
    thread = MagicMock()
    thread.thread_instance = ti
    if status is not None:
        thread.status = status
    return thread


class TestClearAllLabware:

    async def test_clear_all_removes_from_system_and_store(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register(
                "plate_96", barcode="BC-1", confirm=True,
            )
            # Sanity: registered in both system.labwares and the store.
            assert any(lw.id == snap.id for lw in system.labwares)
            assert await runtime.labware._store.get_by_id(snap.id) is not None

            cleared = await runtime.labware.clear_all_labware(force=True)
            assert snap.id in cleared
            assert not [lw for lw in system.labwares if lw.id == snap.id]
            # A lifecycle end is not a retraction: identity survives, only
            # the active position is dropped.
            assert await runtime.labware._store.get_by_id(snap.id) is not None
            assert await runtime.labware._store.list_active_locations() == []
        finally:
            await runtime.shutdown()

    async def test_clear_all_clears_physical_location(self) -> None:
        """When labware sits at a Location.labware slot (the canonical
        case after `initialize_labware`), clear_all must wipe that too."""
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            instance = LabwareInstance("plate_96", "96_well")
            system.add_labware(instance)
            pad1 = system.system_map.get_location("pad1")
            pad1.initialize_labware(instance)
            assert pad1.labware is instance

            await runtime.labware.clear_all_labware(force=True)
            assert pad1.labware is None
            assert not [lw for lw in system.labwares if lw.id == instance.id]
        finally:
            await runtime.shutdown()


class TestClearAllHolderContract:
    """clear_all drives the one reset contract across EVERY labware-state holder
    (devices + transporters), not just transporters; a stateless device no-ops
    and an offline/erroring holder cannot block the panic button."""

    def _add_liquid_handler(self, system: ISystem) -> LiquidHandler:
        lh = LiquidHandler(
            name="lh1", sim=True,
            deck_layout_store=seeded_deck_layout_service(),
        )
        system.add_resource(lh)
        return lh

    async def test_clear_all_resets_every_holder(self) -> None:
        system, _ = await _build_simple_system()
        self._add_liquid_handler(system)
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            reset: list[str] = []

            def _spy(holder: ILabwareStateHolder) -> None:
                async def _record() -> None:
                    reset.append(holder.name)
                # The panic button asks for every world, not just the caller's.
                setattr(holder, "reset_labware_state_everywhere", _record)

            for holder in [*system.devices, *system.movers]:
                _spy(holder)

            await runtime.labware.clear_all_labware(force=True)

            # Walks `movers`, not `transporters`: the production holder list covers
            # device-owned grippers, which are TransporterBase but not Transporter.
            assert set(reset) == {"shaker1", "robot1", "lh1"}
        finally:
            await runtime.shutdown()

    async def test_offline_holder_does_not_block_panic_button(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            instance = LabwareInstance("plate_96", "96_well")
            system.add_labware(instance)
            await runtime.labware._store.register(instance)

            async def _boom() -> None:
                raise RuntimeError("transporter offline")

            setattr(
                system.transporters[0], "reset_labware_state_everywhere", _boom)

            cleared = await runtime.labware.clear_all_labware(force=True)

            assert instance.id in cleared
            assert await runtime.labware._store.get_by_id(instance.id) is not None
            assert await runtime.labware._store.list_active_locations() == []
        finally:
            await runtime.shutdown()

    async def test_transporter_reset_labware_state_delegates_to_reset_world(self) -> None:
        transporter = create_test_transporter("robot1", ["pad1"])
        called: list[bool] = []

        async def _reset_world() -> None:
            called.append(True)

        setattr(transporter, "reset_world", _reset_world)
        await transporter.reset_labware_state()
        assert called == [True]

    async def test_stateless_device_reset_labware_state_is_noop(self) -> None:
        device = create_test_device("shaker1")
        assert await device.reset_labware_state() is None

    async def test_a_mover_clears_both_of_its_worlds(self) -> None:
        """The narrow reset means the world the caller dispatches in. Clear-all
        means both, because the seeds a sim run left are in the sim driver's
        world while an operator's clear-all resolves to LIVE."""
        transporter = create_test_transporter("robot1", ["pad1"])
        cleared: list[int] = []

        def _spy_on(driver: ITransporterDriver) -> None:
            async def _reset(_request: ResetWorldRequest) -> None:
                cleared.append(id(driver))
            setattr(driver, "reset_world", _reset)

        for each in transporter._sim_manager.all_drivers:
            _spy_on(each)

        await transporter.reset_labware_state_everywhere()

        assert len(set(cleared)) == 2, (
            f"clear-all reached {len(set(cleared))} of the mover's two worlds"
        )

    async def test_liquid_handler_reset_labware_state_calls_driver(self) -> None:
        lh = LiquidHandler(
            name="lh1", sim=True,
            deck_layout_store=seeded_deck_layout_service(),
        )
        requests: list[ResetDeckLabwareRequest] = []

        async def _reset_deck(request: ResetDeckLabwareRequest) -> LabwareStateResponse:
            requests.append(request)
            return LabwareStateResponse(success=True)

        setattr(lh.driver, "reset_deck_labware", _reset_deck)
        await lh.reset_labware_state()
        assert len(requests) == 1
        assert isinstance(requests[0], ResetDeckLabwareRequest)


class _StatusUnreadableThread:
    """A thread whose status read misses, as it does mid LIVE-manual-place bind:
    the status registry is keyed on the labware id and the bind swaps it."""

    def __init__(self, labware: LabwareInstance) -> None:
        instance = MagicMock()
        instance.labware = labware
        self.thread_instance = instance

    @property
    def status(self) -> LabwareThreadStatus:
        raise KeyError("no status found for entity")


class TestDischargeLabware:

    async def test_discharge_removes_from_three_stores(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            instance = LabwareInstance("plate_96", "96_well")
            instance.barcode = "BC-2"
            system.add_labware(instance)
            await runtime.labware._store.register(instance)
            pad1 = system.system_map.get_location("pad1")
            pad1.initialize_labware(instance)

            await runtime.labware.discharge_labware(instance.id, force=True)

            assert not [lw for lw in system.labwares if lw.id == instance.id]
            assert pad1.labware is None
            # Discharge closes the labware out; its record stays queryable.
            assert await runtime.labware._store.get_by_id(instance.id) is not None
            assert await runtime.labware._store.list_active_locations() == []
        finally:
            await runtime.shutdown()

    async def test_an_unreadable_thread_status_refuses_instead_of_raising(self) -> None:
        """A thread's status is filed under its labware id and a LIVE manual
        place swaps that id, so the read can miss while the bind is in flight.
        "Cannot tell" has to mean "still holding it": the raise escaped this
        facade as a 404 telling the operator the plate was never registered."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        instance = LabwareInstance("plate_96", "96_well")
        system.add_labware(instance)
        await store.register(instance)

        exec_iter = _StubExecutionIterator(
            [_make_active_execution(
                system, threads_view=[_StatusUnreadableThread(instance)],
            )],
        )
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(ActiveExecutionRefusedError):
            await facade.discharge_labware(instance.id)

    async def test_discharge_unknown_id_raises(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            with pytest.raises(KeyError):
                await runtime.labware.discharge_labware(
                    "does-not-exist", force=False,
                )
        finally:
            await runtime.shutdown()


# -- C2: clear_submission_labware ------------------------------------------


class TestClearSubmissionLabware:
    """`clear_submission_labware` shipped with zero
    tests. The most interesting predicate -- reuse-bound labware is
    skipped -- was never exercised. These tests pin the per-thread
    walk, the reuse-bind skip, and the force-bypass paths.
    """

    async def test_walks_threads_keyed_by_submission_id(self) -> None:
        """Only labware on threads matching submission_id are cleared."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()

        target_lw = LabwareInstance("plate_96", "96_well")
        other_lw = LabwareInstance("plate_96", "96_well")
        for lw in (target_lw, other_lw):
            system.add_labware(lw)
            await store.register(lw)
        pad1 = system.system_map.get_location("pad1")
        pad1.initialize_labware(target_lw)

        threads = [
            _make_thread_stub("sub-target", target_lw),
            _make_thread_stub("sub-other", other_lw),
        ]
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=threads)],
        )
        facade = LabwareFacade(system, store, exec_iter)

        result = await facade.clear_submission_labware(
            "sub-target", force=True,
        )

        assert result.cleared == [target_lw.id]
        assert result.preserved_reuse_bound == []
        assert pad1.labware is None
        assert await store.get_by_id(target_lw.id) is not None
        assert store.get_location(target_lw.id) is None
        # The other submission's labware is untouched.
        assert await store.get_by_id(other_lw.id) is not None

        for t in threads:
            t.thread_instance.thread_template = None  # quiet pending warning
        exec_iter._executions[0].task.cancel()

    async def test_reuse_bound_labware_is_preserved(self) -> None:
        """Threads whose template declared `end_leave_in_place=True`
        are SKIPPED -- the shared reagent survives a clear that targets
        the submission using it."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        reagent = LabwareInstance("reagent_plate", "trough")
        consumable = LabwareInstance("plate_96", "96_well")
        for lw in (reagent, consumable):
            system.add_labware(lw)
            await store.register(lw)

        threads = [
            _make_thread_stub("sub-1", reagent, end_leave_in_place=True),
            _make_thread_stub("sub-1", consumable, end_leave_in_place=False),
        ]
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=threads)],
        )
        facade = LabwareFacade(system, store, exec_iter)

        result = await facade.clear_submission_labware("sub-1", force=True)

        assert result.cleared == [consumable.id]
        assert result.preserved_reuse_bound == [reagent.id]
        assert reagent.id not in result.cleared
        assert await store.get_by_id(reagent.id) is not None
        assert await store.get_by_id(consumable.id) is not None
        assert store.get_location(consumable.id) is None

        exec_iter._executions[0].task.cancel()

    async def test_force_false_refuses_when_execution_active(self) -> None:
        """C3: with an active execution AND force=False, the refusal
        branch must fire. The pre-fix code returned [] because the
        getattr trick made `_iter_active_executions` always empty."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        threads = [_make_thread_stub("sub-1", lw)]
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=threads)],
        )
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(RuntimeError, match="active execution"):
            await facade.clear_submission_labware("sub-1", force=False)

        # Labware untouched.
        assert await store.get_by_id(lw.id) is not None

        exec_iter._executions[0].task.cancel()

    async def test_force_true_bypasses_active_execution(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        threads = [_make_thread_stub("sub-1", lw)]
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=threads)],
        )
        facade = LabwareFacade(system, store, exec_iter)

        result = await facade.clear_submission_labware("sub-1", force=True)

        assert result.cleared == [lw.id]
        assert result.preserved_reuse_bound == []
        assert await store.get_by_id(lw.id) is not None
        assert store.get_location(lw.id) is None

        exec_iter._executions[0].task.cancel()


# -- C3: active-execution refusal pin for discharge + clear_all ------------


class TestActiveExecutionRefusal:
    """Every existing clear-tool test passed
    force=True, so the force=False refusal -- the whole reason the
    guard exists -- was untested. The pre-fix `_iter_active_executions`
    used a getattr shortcut that always returned an empty iterator,
    silently neutering the refusal. These tests would have caught it.
    """

    async def test_discharge_refuses_when_thread_holds_labware(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        threads = [_make_thread_stub("sub-1", lw)]
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=threads)],
        )
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(RuntimeError, match="active thread"):
            await facade.discharge_labware(lw.id, force=False)

        assert await store.get_by_id(lw.id) is not None

        exec_iter._executions[0].task.cancel()

    async def test_discharge_allows_the_thread_waiting_for_that_removal(self) -> None:
        """A thread parked at AWAITING_MANUAL_REMOVE is asking the operator to
        take this labware off, so it must not be the reason the take-off is
        refused. Forcing past the check would have skipped it for every other
        thread too."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        threads = [_make_thread_stub(
            "sub-1", lw, status=LabwareThreadStatus.AWAITING_MANUAL_REMOVE,
        )]
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=threads)],
        )
        facade = LabwareFacade(system, store, exec_iter)

        await facade.discharge_labware(lw.id, force=False)

        assert await store.get_by_id(lw.id) is not None
        assert store.get_location(lw.id) is None

        exec_iter._executions[0].task.cancel()

    async def test_discharge_still_refuses_when_another_thread_holds_it(self) -> None:
        """The exemption is per-thread: a second thread using the same labware
        for real still blocks the discharge."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        threads = [
            _make_thread_stub(
                "sub-1", lw, status=LabwareThreadStatus.AWAITING_MANUAL_REMOVE,
            ),
            _make_thread_stub("sub-2", lw, status=LabwareThreadStatus.EXECUTING_ACTION),
        ]
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=threads)],
        )
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(RuntimeError, match="active thread"):
            await facade.discharge_labware(lw.id, force=False)

        assert await store.get_by_id(lw.id) is not None

        exec_iter._executions[0].task.cancel()

    async def test_clear_all_refuses_when_any_execution_active(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=[])],
        )
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(RuntimeError, match="active execution"):
            await facade.clear_all_labware(force=False)

        assert await store.get_by_id(lw.id) is not None

        exec_iter._executions[0].task.cancel()

    async def test_discharge_allows_when_no_active_executions(self) -> None:
        """No active execution -> force=False discharge succeeds."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        exec_iter = _StubExecutionIterator([])  # zero active executions
        facade = LabwareFacade(system, store, exec_iter)

        await facade.discharge_labware(lw.id, force=False)

        assert await store.get_by_id(lw.id) is not None
        assert store.get_location(lw.id) is None

    async def test_clear_all_allows_when_no_active_executions(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        exec_iter = _StubExecutionIterator([])
        facade = LabwareFacade(system, store, exec_iter)

        cleared = await facade.clear_all_labware(force=False)

        assert lw.id in cleared


class TestActiveExecutionRefusalEndToEnd:
    """The C3 safety net for the C1 bug. The unit tests above construct
    `LabwareFacade` directly with a stub iterator, which always exercises
    the typed surface. The C1 bug was at the SystemRuntime wiring boundary:
    facade was passed `system: ISystem` and silently lost its iterator
    because `iter_executions` lived on ISystemRuntime, not ISystem.

    To regress that bug we must exercise the production wiring, with an
    active execution actually tracked by the runtime.
    """

    async def test_force_false_refusal_fires_through_runtime_wiring(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            lw = LabwareInstance("plate_96", "96_well")
            system.add_labware(lw)
            await runtime.labware._store.register(lw)

            # Inject an Execution into the runtime's execution map. The
            # facade obtained via `runtime.labware` must see it through
            # the IExecutionIterator wiring established at construction.
            task = _make_pending_task()
            execution = Execution(
                id="injected-ex",
                workflow_name="injected",
                workflow=MagicMock(),
                system=system,
                task=task,
                phase=ExecutionPhase.ACCEPTING,
            )
            ew = MagicMock()
            ew.threads = [_make_thread_stub("sub-injected", lw)]
            execution.executing_workflow = ew
            runtime._executions[execution.id] = execution
            try:
                with pytest.raises(RuntimeError, match="active execution"):
                    await runtime.labware.clear_all_labware(force=False)
                with pytest.raises(RuntimeError, match="active thread"):
                    await runtime.labware.discharge_labware(lw.id, force=False)
                with pytest.raises(RuntimeError, match="active execution"):
                    await runtime.labware.clear_submission_labware(
                        "sub-injected", force=False,
                    )
            finally:
                task.cancel()
                runtime._executions.pop(execution.id, None)
        finally:
            await runtime.shutdown()


class TestTypedExceptions:
    """L7: bare RuntimeError / KeyError replaced with typed subclasses so
    a hosted wire layer can map to CONFLICT / LABWARE_NOT_FOUND envelopes
    without isinstance-on-RuntimeError or string sniffing.
    """

    async def test_clear_submission_raises_active_execution_refused(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)
        threads = [_make_thread_stub("sub-x", lw)]
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=threads)],
        )
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(ActiveExecutionRefusedError) as excinfo:
            await facade.clear_submission_labware("sub-x", force=False)

        assert excinfo.value.scope == "submission"
        assert excinfo.value.submission_id == "sub-x"
        exec_iter._executions[0].task.cancel()

    async def test_discharge_raises_active_execution_refused(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)
        threads = [_make_thread_stub("sub-x", lw)]
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=threads)],
        )
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(ActiveExecutionRefusedError) as excinfo:
            await facade.discharge_labware(lw.id, force=False)

        assert excinfo.value.scope == "labware"
        assert excinfo.value.labware_id == lw.id
        exec_iter._executions[0].task.cancel()

    async def test_clear_all_raises_active_execution_refused(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        exec_iter = _StubExecutionIterator(
            [_make_active_execution(system, threads_view=[])],
        )
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(ActiveExecutionRefusedError) as excinfo:
            await facade.clear_all_labware(force=False)

        assert excinfo.value.scope == "all"
        assert excinfo.value.submission_id is None
        assert excinfo.value.labware_id is None
        exec_iter._executions[0].task.cancel()

    async def test_discharge_unknown_id_raises_labware_not_found(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        exec_iter = _StubExecutionIterator([])
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(LabwareNotFoundError) as excinfo:
            await facade.discharge_labware("does-not-exist", force=False)

        assert excinfo.value.labware_id == "does-not-exist"

    def test_active_execution_refused_subclasses_runtime_error(self) -> None:
        """Backward-compat: callers catching RuntimeError keep working."""
        assert issubclass(ActiveExecutionRefusedError, RuntimeError)

    def test_labware_not_found_subclasses_key_error(self) -> None:
        """Backward-compat: callers catching KeyError keep working."""
        assert issubclass(LabwareNotFoundError, KeyError)


class TestClearSubmissionTerminalExecution:
    """`clear_submission_labware` against a run that has already finished.

    This used to return an empty result, on the reading that the verb was for
    aborting a submission still in flight and that a finished run's leftovers
    were a job for `discharge_labware` one plate at a time or the `clear_all`
    panic button. That reading was wrong twice over: clearing a deck is
    something an operator does when the plates have stopped moving, so the
    finished run is the ordinary case rather than an edge one, and `clear_all`
    is not a substitute because it also wipes the deck residents whose
    identities carry their volume and remaining tips into the next run.
    """

    async def test_a_finished_run_is_the_ordinary_case_to_clear(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        # Build an execution whose task is ALREADY DONE.
        async def _done() -> None:
            return None
        loop = asyncio.get_event_loop()
        completed_task = loop.create_task(_done())
        await completed_task
        assert completed_task.done()

        threads = [_make_thread_stub("sub-x", lw)]
        ew = MagicMock()
        ew.threads = threads
        execution = Execution(
            id="ex-terminal",
            workflow_name="wf",
            workflow=MagicMock(),
            system=system,
            task=completed_task,
            phase=ExecutionPhase.COMPLETED,
            executing_workflow=ew,
        )
        exec_iter = _StubExecutionIterator([execution])
        facade = LabwareFacade(system, store, exec_iter)

        # No refusal: nothing is still carrying the plate.
        result = await facade.clear_submission_labware("sub-x", force=False)

        assert result.cleared == [lw.id]
        assert not [held for held in system.labwares if held.id == lw.id], (
            "the plate is off the system"
        )
        assert store.get_location(lw.id) is None, "and off the deck"
        assert await store.get_by_id(lw.id) is not None, (
            "a clear is a close-out, not a deletion: the run's record stays"
        )

    async def test_another_run_in_flight_does_not_block_the_clear(self) -> None:
        """The refusal is about the submission's own run, not about the lab
        being busy. A second workflow running elsewhere must not stop an
        operator clearing the bench a finished run left behind."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        finished_lw = LabwareInstance("plate_96", "96_well")
        busy_lw = LabwareInstance("plate_96", "96_well")
        for lw in (finished_lw, busy_lw):
            system.add_labware(lw)
            await store.register(lw)

        async def _done() -> None:
            return None
        loop = asyncio.get_event_loop()
        completed_task = loop.create_task(_done())
        await completed_task

        finished_ew = MagicMock()
        finished_ew.threads = [_make_thread_stub("sub-finished", finished_lw)]
        finished = Execution(
            id="ex-finished",
            workflow_name="wf",
            workflow=MagicMock(),
            system=system,
            task=completed_task,
            phase=ExecutionPhase.COMPLETED,
            executing_workflow=finished_ew,
        )
        still_running = _make_active_execution(
            system, threads_view=[_make_thread_stub("sub-busy", busy_lw)],
        )
        exec_iter = _StubExecutionIterator([finished, still_running])
        facade = LabwareFacade(system, store, exec_iter)

        try:
            result = await facade.clear_submission_labware(
                "sub-finished", force=False,
            )
        finally:
            still_running.task.cancel()

        assert result.cleared == [finished_lw.id]
        assert [held for held in system.labwares if held.id == busy_lw.id], (
            "the running submission's plate is untouched"
        )

    async def test_terminal_execution_does_not_block_clear_all(self) -> None:
        """A terminal execution must not block clear_all (force=False)
        since `_has_any_active_thread` walks the same iterator."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        lw = LabwareInstance("plate_96", "96_well")
        system.add_labware(lw)
        await store.register(lw)

        async def _done() -> None:
            return None
        loop = asyncio.get_event_loop()
        completed_task = loop.create_task(_done())
        await completed_task

        execution = Execution(
            id="ex-terminal",
            workflow_name="wf",
            workflow=MagicMock(),
            system=system,
            task=completed_task,
            phase=ExecutionPhase.COMPLETED,
        )
        exec_iter = _StubExecutionIterator([execution])
        facade = LabwareFacade(system, store, exec_iter)

        cleared = await facade.clear_all_labware(force=False)

        assert lw.id in cleared


class TestClearAllAuthoritative:
    """clear_all_labware is the panic button: after it runs, no positional or
    identity state survives -- not in the engine registry, not as a
    persistent-store phantom that would rehydrate on boot, not in the location
    ledger, and not as a seed in a transporter's world projection. (Append-only
    audit history is intentionally retained; see ILabwareStore.delete.) These
    pin each layer.
    """

    async def test_clears_position_of_store_phantom_but_keeps_its_row(self) -> None:
        """A positioned store row the in-memory registry never knew about
        (an orphan a failed/aborted execution left behind) must lose its
        active position, else it rehydrates and re-seeds projections on the
        next boot. Its identity row survives: clears are not retractions."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        phantom = LabwareInstance("plate_96", "96_well")
        await store.register(phantom)
        await store.update_location(phantom.id, "pad1")
        assert (phantom.id, "pad1") in await store.list_active_locations()
        assert not [lw for lw in system.labwares if lw.id == phantom.id]

        facade = LabwareFacade(system, store, _StubExecutionIterator([]))
        cleared = await facade.clear_all_labware(force=True)

        assert phantom.id in cleared
        assert await store.list_active_locations() == []
        assert await store.get_by_id(phantom.id) is not None

    async def test_wipes_location_service_ledger(self) -> None:
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        instance = LabwareInstance("plate_96", "96_well")
        system.add_labware(instance)
        await store.register(instance)
        pad1 = system.system_map.get_location("pad1")
        system.labware_location_service.update(instance, pad1)
        assert system.labware_location_service.get_all()

        facade = LabwareFacade(system, store, _StubExecutionIterator([]))
        await facade.clear_all_labware(force=True)

        assert system.labware_location_service.get_all() == {}

    async def test_resets_transporter_world_projection(self) -> None:
        """The user's exact failure: a stale seed in the transporter graph
        survives `clear`. After the authoritative clear the position is free
        -- proven by a fresh seed succeeding where it would have raised
        'already occupied'."""
        from cheshire_drivers.labware_models import LabwareIdentity
        from cheshire_drivers.transporter_models import SeedPositionRequest

        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        transporter = system.transporters[0]
        await transporter.driver.seed_position(
            SeedPositionRequest(
                position_id="pad1",
                labware=LabwareIdentity(
                    labware_id="orphan", barcode=None, labware_type="x",
                ),
            )
        )
        assert transporter.driver._resource_at("pad1") is not None

        facade = LabwareFacade(system, store, _StubExecutionIterator([]))
        await facade.clear_all_labware(force=True)

        assert transporter.driver._resource_at("pad1") is None
        assert transporter.driver._resources_by_id == {}
        # Operator-visible proof: the position is reusable, no stale veto.
        await transporter.driver.seed_position(
            SeedPositionRequest(
                position_id="pad1",
                labware=LabwareIdentity(
                    labware_id="fresh", barcode=None, labware_type="x",
                ),
            )
        )

    async def test_panic_button_survives_transporter_reset_failure(self) -> None:
        """An offline/erroring transporter must not block the panic button:
        the rest of the clear still completes."""
        system, _ = await _build_simple_system()
        store = InMemoryLabwareStore()
        phantom = LabwareInstance("plate_96", "96_well")
        await store.register(phantom)
        await store.update_location(phantom.id, "pad1")

        async def _boom() -> None:
            raise RuntimeError("transporter offline")
        system.transporters[0].reset_world = _boom

        facade = LabwareFacade(system, store, _StubExecutionIterator([]))
        cleared = await facade.clear_all_labware(force=True)

        assert phantom.id in cleared
        assert await store.list_active_locations() == []


class _RehydratingLabwareStore(InMemoryLabwareStore):
    """Stand-in for a hosted deployment's DbLabwareStore, which reads rows, not objects.

    Every lookup returns a FRESH LabwareInstance carrying the persisted
    identity, never the object the engine is holding.
    """

    async def get_by_id(self, labware_id: str) -> LabwareInstance | None:
        found = await super().get_by_id(labware_id)
        if found is None:
            return None
        return LabwareInstance(
            template_name=found.template_name,
            labware_type=found.labware_type,
            barcode=found.barcode,
            instance_id=found.id,
            name=found.name,
        )


class TestDischargeAgainstARehydratingStore:
    """The only deployment that ships a real store rehydrates on every read.

    Every discharge test above uses the in-memory store, which hands back the
    very object the engine holds, so an identity comparison looks correct there
    and fails everywhere it matters.
    """

    async def test_discharge_clears_the_slot(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=_RehydratingLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            pad1 = system.system_map.get_location("pad1")
            assert pad1.labware is not None

            await runtime.labware.discharge_labware(snap.id)

            # A manual-remove park polls exactly this; leaving it set parks
            # the thread forever with the registry row already gone.
            assert pad1.labware is None
        finally:
            await runtime.shutdown()

    async def test_a_barcode_edit_reaches_the_instance_the_engine_holds(self) -> None:
        """Same defect, a different operator verb: the edit landed on a copy
        built for the call, so the store recorded the new barcode while every
        in-memory read (join-by-barcode, snapshots) kept the old one."""
        system, _ = await _build_simple_system()
        store = _RehydratingLabwareStore()
        instance = LabwareInstance("plate_96", "96_well", barcode="BC-OLD")
        system.add_labware(instance)
        await store.register(instance)
        facade = LabwareFacade(system, store, _StubExecutionIterator([]))

        await facade.edit_barcode(instance.id, "BC-NEW", confirm=True)

        assert instance.barcode == "BC-NEW"

    async def test_a_thread_still_holding_the_plate_still_refuses(self) -> None:
        system, _ = await _build_simple_system()
        store = _RehydratingLabwareStore()
        held = LabwareInstance("plate_96", "96_well")
        system.add_labware(held)
        await store.register(held)
        system.system_map.get_location("pad1").initialize_labware(held)

        exec_iter = _StubExecutionIterator([
            _make_active_execution(system, threads_view=[
                _make_thread_stub(
                    "sub-1", held, status=LabwareThreadStatus.EXECUTING_ACTION,
                ),
            ]),
        ])
        facade = LabwareFacade(system, store, exec_iter)

        with pytest.raises(ActiveExecutionRefusedError):
            await facade.discharge_labware(held.id)
