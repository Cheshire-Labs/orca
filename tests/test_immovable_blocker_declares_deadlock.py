"""Round-1 tests for the immovable-blocker unresolvable-deadlock path.

The existing `ThreadDeadlockDetector` only inspects threads with reservation
collections IN the current tick's queue. A thread parked on `orca.join`
outside the queue is invisible to that path: when it holds labware blocking
another thread's reservation request, the wait-for graph never sees the
edge and no deadlock is declared. The system silently stalls.

These tests cover the Round-1 surface that closes the gap:

* `find_unresolvable_blocker` consults the FULL thread registry (not just
  labwares-in-queue) and declares the deadlock ONLY when the blocker's
  thread template has `immovable=True`.
* Both `LocationCollectionReservationRequest` and
  `MoveActionCollectionReservationRequest` expose the new
  `unresolvable_deadlock` event + context, reset on `clear()`.
* `WorkflowTemplate.add_thread` refuses `immovable=True` at `wf.start()`
  (symmetric with `ReuseThreadCannotBeEntryError`).
* `UnresolvableDeadlockError` carries a populated context dataclass.

The end-to-end pause + incident wiring sits in the next layer; these
tests pin the per-piece behavior.
"""
from unittest.mock import Mock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.system.reservation_manager.deadlock_manager import (
    DeadlockStarvationRegistry,
    ThreadDeadlockDetector,
)
from orca.runtime.db import create_memory_engine
from orca.runtime.incident_service import IncidentService
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentSeverity,
    RecoverableTimeoutContext,
    RecoveryAction,
    SystemIncident,
)
from orca.runtime.recoverable_timeout import RecoverableTimeoutCoordinator
from orca.system.reservation_manager.errors import (
    IThreadIncidentDeclarer,
    OrphanedBacklogContext,
    UnresolvableDeadlockContext,
    UnresolvableDeadlockError,
)
from orca.system.reservation_manager.move_handler import (
    MoveActionCollectionReservationRequest,
)
from orca.system.thread_registry_interface import IThreadRegistry
from orca.workflow_models.actions.move_action import MoveAction
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_labware(name: str) -> LabwareInstance:
    return LabwareInstance(name, "plate")


def _make_location(name: str) -> Location:
    pad = PlatePad(name, supports_deadlock_resolution=True)
    return Location(name, pad)


def _place_labware(location: Location, labware: LabwareInstance) -> None:
    location.resource.initialize_labware(labware)


def _make_mock_thread_with_template(
    labware: LabwareInstance,
    *,
    immovable: bool,
) -> Mock:
    """Create a mock LabwareThreadInstance whose template has the given immovable flag."""
    thread = Mock()
    thread.labware = labware
    thread.id = labware.id
    thread.name = f"thread_for_{labware.template_name}"
    template = Mock()
    template.immovable = immovable
    thread.thread_template = template
    return thread


def _make_mock_thread_no_template(labware: LabwareInstance) -> Mock:
    """Thread whose template is None (transient state)."""
    thread = Mock()
    thread.labware = labware
    thread.id = labware.id
    thread.name = f"thread_for_{labware.template_name}"
    thread.thread_template = None
    return thread


def _make_thread_registry(
    by_id: dict[str, Mock],
    by_labware: dict[str, Mock] | None = None,
) -> IThreadRegistry:
    reg = Mock(spec=IThreadRegistry)
    reg.get_thread = Mock(side_effect=lambda tid: by_id.get(tid))
    lookup = by_labware if by_labware is not None else {}

    def _get_by_labware(labware_id: str) -> Mock:
        if labware_id in lookup:
            return lookup[labware_id]
        raise KeyError(f"No thread found for labware {labware_id}")

    reg.get_thread_by_labware = Mock(side_effect=_get_by_labware)
    reg.threads = list(by_id.values())
    return reg


def _make_move_action(
    labware: LabwareInstance, source: Location, target: Location,
) -> MoveAction:
    transporter = Mock()
    transporter.name = "mock_transporter"
    return MoveAction(labware, source, target, transporter)


def _make_move_collection(
    thread_id: str,
    labware: LabwareInstance,
    source: Location,
    target: Location,
) -> MoveActionCollectionReservationRequest:
    move = _make_move_action(labware, source, target)
    return MoveActionCollectionReservationRequest(thread_id, [move])


# ---------------------------------------------------------------------------
# UnresolvableDeadlockError + context shape
# ---------------------------------------------------------------------------


class TestUnresolvableDeadlockError:

    def test_context_fields_populated(self) -> None:
        """All seven context fields land on the error object."""
        ctx = UnresolvableDeadlockContext(
            requesting_thread_id="entry",
            requesting_labware_id="entry_lw",
            blocking_position_id="lh",
            blocking_thread_id="blocker",
            blocking_labware_id="blocker_lw",
            reason="blocking thread declared immovable=True",
            hint="Set immovable=False if movable.",
        )
        err = UnresolvableDeadlockError(ctx)
        assert err.context is ctx
        assert err.context.requesting_thread_id == "entry"
        assert err.context.blocking_position_id == "lh"
        assert err.context.reason == "blocking thread declared immovable=True"

    def test_message_includes_chain(self) -> None:
        """The default message names both threads and the location."""
        ctx = UnresolvableDeadlockContext(
            requesting_thread_id="entry",
            requesting_labware_id="entry_lw",
            blocking_position_id="lh",
            blocking_thread_id="blocker",
            blocking_labware_id="blocker_lw",
            reason="blocking thread declared immovable=True",
            hint="hint",
        )
        msg = str(UnresolvableDeadlockError(ctx))
        assert "entry" in msg
        assert "blocker" in msg
        assert "lh" in msg
        assert "immovable=True" in msg

    def test_error_inherits_runtime_error(self) -> None:
        """Subclasses RuntimeError so it propagates through `except Exception` catches."""
        ctx = UnresolvableDeadlockContext(
            requesting_thread_id="r",
            requesting_labware_id="rlw",
            blocking_position_id="loc",
            blocking_thread_id="b",
            blocking_labware_id="blw",
            reason="r",
            hint="h",
        )
        assert isinstance(UnresolvableDeadlockError(ctx), RuntimeError)


# ---------------------------------------------------------------------------
# find_unresolvable_blocker: positive case
# ---------------------------------------------------------------------------


class TestFindUnresolvableBlocker:
    """Detector method tests — full registry lookup, immovable gate, edge cases."""

    def test_immovable_blocker_returns_context(self) -> None:
        """Out-of-queue blocker with immovable=True → context populated."""
        # Setup: entry thread requests `lh` which is occupied by dmso's labware.
        entry_lw = _make_labware("entry_plate")
        dmso_lw = _make_labware("dmso_reservoir")
        lh = _make_location("lh")
        _place_labware(lh, dmso_lw)
        src = _make_location("stacker")

        entry_thread = _make_mock_thread_with_template(entry_lw, immovable=False)
        dmso_thread = _make_mock_thread_with_template(dmso_lw, immovable=True)

        # CRITICAL: dmso_thread is NOT in the by_id map (parked outside queue)
        # but IS reachable via get_thread_by_labware. This is the gap the new
        # detector path closes.
        thread_reg = _make_thread_registry(
            by_id={"entry": entry_thread},
            by_labware={dmso_lw.id: dmso_thread},
        )

        collection = _make_move_collection("entry", entry_lw, src, lh)
        collection.rejected.set()  # detector only inspects rejected collections

        detector = ThreadDeadlockDetector(thread_reg, DeadlockStarvationRegistry(), reservation_at=lambda _position_id: None)
        ctx = detector.find_unresolvable_blocker(collection)

        assert ctx is not None
        assert ctx.requesting_thread_id == "entry"
        assert ctx.requesting_labware_id == entry_lw.id
        assert ctx.blocking_position_id == "lh"
        assert ctx.blocking_thread_id == dmso_lw.id
        assert ctx.blocking_labware_id == dmso_lw.id
        assert "immovable=True" in ctx.reason
        assert "REUSE_EXISTING" in ctx.hint

    def test_movable_blocker_returns_none(self) -> None:
        """Same scenario but blocker has immovable=False → no declaration."""
        entry_lw = _make_labware("entry_plate")
        dmso_lw = _make_labware("dmso_reservoir")
        lh = _make_location("lh")
        _place_labware(lh, dmso_lw)
        src = _make_location("stacker")

        entry_thread = _make_mock_thread_with_template(entry_lw, immovable=False)
        dmso_thread = _make_mock_thread_with_template(dmso_lw, immovable=False)

        thread_reg = _make_thread_registry(
            by_id={"entry": entry_thread},
            by_labware={dmso_lw.id: dmso_thread},
        )

        collection = _make_move_collection("entry", entry_lw, src, lh)
        collection.rejected.set()

        detector = ThreadDeadlockDetector(thread_reg, DeadlockStarvationRegistry(), reservation_at=lambda _position_id: None)
        assert detector.find_unresolvable_blocker(collection) is None

    def test_unmarked_collection_returns_none(self) -> None:
        """Detector only inspects rejected collections — granted/empty skipped."""
        entry_lw = _make_labware("entry_plate")
        dmso_lw = _make_labware("dmso_reservoir")
        lh = _make_location("lh")
        _place_labware(lh, dmso_lw)
        src = _make_location("stacker")

        entry_thread = _make_mock_thread_with_template(entry_lw, immovable=False)
        dmso_thread = _make_mock_thread_with_template(dmso_lw, immovable=True)

        thread_reg = _make_thread_registry(
            by_id={"entry": entry_thread},
            by_labware={dmso_lw.id: dmso_thread},
        )

        collection = _make_move_collection("entry", entry_lw, src, lh)
        # Intentionally do NOT set collection.rejected — the detector should skip.

        detector = ThreadDeadlockDetector(thread_reg, DeadlockStarvationRegistry(), reservation_at=lambda _position_id: None)
        assert detector.find_unresolvable_blocker(collection) is None

    def test_handles_keyerror_when_blocker_not_in_registry(self) -> None:
        """Labware sitting at the slot but not owned by any thread → skip, no crash."""
        entry_lw = _make_labware("entry_plate")
        orphan_lw = _make_labware("orphan_plate")  # belongs to no thread
        lh = _make_location("lh")
        _place_labware(lh, orphan_lw)
        src = _make_location("stacker")

        entry_thread = _make_mock_thread_with_template(entry_lw, immovable=False)
        # NOTE: orphan_lw.id NOT in by_labware → get_thread_by_labware raises KeyError
        thread_reg = _make_thread_registry(
            by_id={"entry": entry_thread},
            by_labware={},
        )

        collection = _make_move_collection("entry", entry_lw, src, lh)
        collection.rejected.set()

        detector = ThreadDeadlockDetector(thread_reg, DeadlockStarvationRegistry(), reservation_at=lambda _position_id: None)
        # KeyError is swallowed; no declaration.
        assert detector.find_unresolvable_blocker(collection) is None

    def test_handles_none_template(self) -> None:
        """Blocker thread exists but its template is None (transient) → skip."""
        entry_lw = _make_labware("entry_plate")
        dmso_lw = _make_labware("dmso_reservoir")
        lh = _make_location("lh")
        _place_labware(lh, dmso_lw)
        src = _make_location("stacker")

        entry_thread = _make_mock_thread_with_template(entry_lw, immovable=False)
        templateless = _make_mock_thread_no_template(dmso_lw)

        thread_reg = _make_thread_registry(
            by_id={"entry": entry_thread},
            by_labware={dmso_lw.id: templateless},
        )

        collection = _make_move_collection("entry", entry_lw, src, lh)
        collection.rejected.set()

        detector = ThreadDeadlockDetector(thread_reg, DeadlockStarvationRegistry(), reservation_at=lambda _position_id: None)
        # Conservative: missing template means we cannot prove immovable; skip.
        assert detector.find_unresolvable_blocker(collection) is None

    def test_requesting_thread_missing_from_registry_returns_none(self) -> None:
        """If the requester itself is not in registry, abort cleanly."""
        entry_lw = _make_labware("entry_plate")
        dmso_lw = _make_labware("dmso_reservoir")
        lh = _make_location("lh")
        _place_labware(lh, dmso_lw)
        src = _make_location("stacker")

        dmso_thread = _make_mock_thread_with_template(dmso_lw, immovable=True)
        thread_reg = _make_thread_registry(
            by_id={},  # entry not registered
            by_labware={dmso_lw.id: dmso_thread},
        )

        collection = _make_move_collection("entry", entry_lw, src, lh)
        collection.rejected.set()

        detector = ThreadDeadlockDetector(thread_reg, DeadlockStarvationRegistry(), reservation_at=lambda _position_id: None)
        assert detector.find_unresolvable_blocker(collection) is None


# ---------------------------------------------------------------------------
# IReservationCollection event surface
# ---------------------------------------------------------------------------


class TestUnresolvableDeadlockEventSurface:
    """The new event + context property must be present on both collection
    implementations, settable atomically, and reset by `clear()`."""

    def _make_ctx(self) -> UnresolvableDeadlockContext:
        return UnresolvableDeadlockContext(
            requesting_thread_id="r",
            requesting_labware_id="rlw",
            blocking_position_id="loc",
            blocking_thread_id="b",
            blocking_labware_id="blw",
            reason="r",
            hint="h",
        )

    def test_move_collection_event_initially_unset(self) -> None:
        lw = _make_labware("p")
        src = _make_location("a")
        dst = _make_location("b")
        col = _make_move_collection("t", lw, src, dst)
        assert not col.unresolvable_deadlock.is_set()
        assert col.unresolvable_deadlock_context is None

    def test_move_collection_set_unresolvable_deadlock(self) -> None:
        lw = _make_labware("p")
        src = _make_location("a")
        dst = _make_location("b")
        col = _make_move_collection("t", lw, src, dst)
        ctx = self._make_ctx()
        col.set_unresolvable_deadlock(ctx)
        assert col.unresolvable_deadlock.is_set()
        assert col.unresolvable_deadlock_context is ctx

    def test_move_collection_clear_resets_event(self) -> None:
        lw = _make_labware("p")
        src = _make_location("a")
        dst = _make_location("b")
        col = _make_move_collection("t", lw, src, dst)
        col.set_unresolvable_deadlock(self._make_ctx())
        col.clear()
        assert not col.unresolvable_deadlock.is_set()
        assert col.unresolvable_deadlock_context is None

    def test_location_collection_event_present(self) -> None:
        """The action-side collection also exposes the new event."""
        from orca.system.reservation_manager.location_reservation import LocationReservation
        from orca.system.system_map import SystemMap
        from orca.workflow_models.actions.util import LocationCollectionReservationRequest

        ref = _make_location("ref")
        dst = _make_location("dst")
        # Minimal scaffolding — system_map is only used by sorting / distance,
        # which the event-property tests don't invoke.
        system_map = Mock(spec=SystemMap)
        reservation = LocationReservation(dst)
        col = LocationCollectionReservationRequest("t", [reservation], system_map, ref)

        assert not col.unresolvable_deadlock.is_set()
        assert col.unresolvable_deadlock_context is None

        ctx = self._make_ctx()
        col.set_unresolvable_deadlock(ctx)
        assert col.unresolvable_deadlock.is_set()
        assert col.unresolvable_deadlock_context is ctx

        col.clear()
        assert not col.unresolvable_deadlock.is_set()
        assert col.unresolvable_deadlock_context is None


# ---------------------------------------------------------------------------
# Build-time guard: immovable at wf.start() refused
# ---------------------------------------------------------------------------


class TestImmovableEntryGuard:

    def test_immovable_thread_refused_at_workflow_start(self) -> None:
        """WorkflowTemplate.add_thread refuses immovable=True at wf.start()."""
        from orca.resource_models.labware import PlateTemplate as LabwareTemplate
        from orca.runtime.runtime_interface import ImmovableThreadCannotBeEntryError
        from orca.workflow_models.thread_template import ThreadTemplate
        from orca.workflow_models.workflow_templates import WorkflowTemplate

        labware = LabwareTemplate("reagent", "plate")

        async def _body(ctx):  # pragma: no cover - body never executed
            yield

        thread = ThreadTemplate(
            labware_template=labware,
            start="lh",
            end="lh",
            func=_body,
            immovable=True,
        )
        wf = WorkflowTemplate("test_wf")

        with pytest.raises(ImmovableThreadCannotBeEntryError):
            wf.add_thread(thread, is_start=True)

    def test_immovable_thread_allowed_at_wf_thread(self) -> None:
        """Same thread is accepted via wf.thread() (non-entry registration)."""
        from orca.resource_models.labware import PlateTemplate as LabwareTemplate
        from orca.workflow_models.thread_template import ThreadTemplate
        from orca.workflow_models.workflow_templates import WorkflowTemplate

        labware = LabwareTemplate("reagent", "plate")

        async def _body(ctx):  # pragma: no cover
            yield

        thread = ThreadTemplate(
            labware_template=labware,
            start="lh",
            end="lh",
            func=_body,
            immovable=True,
        )
        wf = WorkflowTemplate("test_wf")
        # Should not raise.
        wf.add_thread(thread, is_start=False)


# ---------------------------------------------------------------------------
# ThreadTemplate.immovable plumbing
# ---------------------------------------------------------------------------


class TestThreadTemplateImmovable:

    def test_default_false(self) -> None:
        from orca.resource_models.labware import PlateTemplate as LabwareTemplate
        from orca.workflow_models.thread_template import ThreadTemplate

        labware = LabwareTemplate("plate", "plate")

        async def _body(ctx):  # pragma: no cover
            yield

        thread = ThreadTemplate(
            labware_template=labware,
            start="loc",
            end="loc",
            func=_body,
        )
        assert thread.immovable is False

    def test_explicit_true(self) -> None:
        from orca.resource_models.labware import PlateTemplate as LabwareTemplate
        from orca.workflow_models.thread_template import ThreadTemplate

        labware = LabwareTemplate("reagent", "plate")

        async def _body(ctx):  # pragma: no cover
            yield

        thread = ThreadTemplate(
            labware_template=labware,
            start="lh",
            end="lh",
            func=_body,
            immovable=True,
        )
        assert thread.immovable is True


# ---------------------------------------------------------------------------
# Round 1.5: declarer wiring + auto-invocation from the typed catch
# ---------------------------------------------------------------------------


class _FakeDeclarer:
    """Records declarer calls for verification (full IThreadIncidentDeclarer)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, UnresolvableDeadlockContext]] = []
        self.action_failure_calls: list[tuple[str, str]] = []
        self._incident_store = IncidentService(SqliteIncidentStore(create_memory_engine()))
        self._coordinator = RecoverableTimeoutCoordinator(self)

    def declare_unresolvable_deadlock(
        self, execution_id: str, context: UnresolvableDeadlockContext,
    ) -> object:
        self.calls.append((execution_id, context))
        return object()

    def declare_action_failure(
        self, execution_id: str, thread_id: str, context: object,
    ) -> object:
        self.action_failure_calls.append((execution_id, thread_id))
        return object()

    def declare_orphaned_backlog(
        self, execution_id: str, context: OrphanedBacklogContext,
    ) -> None:
        return None

    # -- IRecoverableTimeoutHost (so the coordinator can construct) + the
    # back-ref property threads read at start. Unused by these plumbing tests.
    def declare_recoverable_timeout(
        self, execution_id: str, context: RecoverableTimeoutContext,
    ) -> SystemIncident:
        return self._incident_store.record(
            category=IncidentCategory.RECOVERABLE_TIMEOUT,
            severity=IncidentSeverity.WARNING,
            message="recoverable timeout",
            detail=context,
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
        )

    def resume_all_threads(self, execution_id: str) -> dict[str, int]:
        return {}

    def acknowledge_incident(self, incident_id: str) -> bool:
        return True

    @property
    def recoverable_timeout_coordinator(self) -> RecoverableTimeoutCoordinator:
        return self._coordinator

    def declare_unresolved_anchor_insert(
        self, execution_id: str, thread_id: str, anchor_name: str,
        direction: str, target_type: str, item_name: str | None,
        anchor_reached: bool,
    ) -> object:
        return object()


class TestDeclarerPlumbing:
    """ExecutingThreadFactory propagates the declarer to threads it creates."""

    def test_factory_default_declarer_is_none(self) -> None:
        from orca.workflow_models.labware_threads.executing_labware_thread import (
            ExecutingThreadFactory,
        )
        factory = ExecutingThreadFactory.__new__(ExecutingThreadFactory)
        factory._thread_incident_declarer = None
        assert factory._thread_incident_declarer is None

    async def test_factory_set_thread_incident_declarer_reaches_created_threads(self) -> None:
        """The declarer set on the factory must reach threads it mints."""
        from unittest.mock import MagicMock
        from orca.workflow_models.labware_threads.executing_labware_thread import (
            ExecutingThreadFactory,
        )
        factory = ExecutingThreadFactory.__new__(ExecutingThreadFactory)
        factory._thread_incident_declarer = None
        for field in (
            "_event_bus", "_move_handler", "_status_manager", "_actions_resolver",
            "_labware_location_service", "_coordination_config", "_method_registry",
            "_executing_method_registry", "_variable_store", "_register_method_template",
            "_labware_placer",
        ):
            setattr(factory, field, MagicMock())
        declarer = _FakeDeclarer()

        factory.set_thread_incident_declarer(declarer)

        instance = MagicMock()
        instance.yield_func = MagicMock()
        thread = factory.create_instance(instance, MagicMock())

        assert thread._thread_incident_declarer is declarer

    def test_registry_set_thread_incident_declarer_forwards_to_factory(self) -> None:
        from orca.workflow_models.labware_threads.executing_labware_thread import (
            ExecutingThreadFactory,
            ExecutingThreadRegistry,
        )
        factory = ExecutingThreadFactory.__new__(ExecutingThreadFactory)
        factory._thread_incident_declarer = None
        registry = ExecutingThreadRegistry.__new__(ExecutingThreadRegistry)
        registry._factory = factory
        declarer = _FakeDeclarer()

        registry.set_thread_incident_declarer(declarer)

        assert factory._thread_incident_declarer is declarer


class TestSystemRuntimeWiresSelfAsDeclarer:
    """SystemRuntime.__init__ calls system.set_thread_incident_declarer(self)."""

    def test_runtime_init_calls_set_thread_incident_declarer_with_self(self) -> None:
        """Drive REAL `SystemRuntime.__init__` and verify it called
        `system.set_thread_incident_declarer(self)`.

        Uses a `MagicMock` for ``system`` so every attribute / method
        access (set_executing_workflow_factory_refs, ops_history,
        variable_store, etc.) returns a sensible mock that doesn't
        block __init__. The test fails if a future edit removes the
        `self._system.set_thread_incident_declarer(self)` line from __init__.
        """
        from unittest.mock import MagicMock

        from orca.runtime.system_runtime import SystemRuntime

        system = MagicMock()

        runtime = SystemRuntime(system)

        system.set_thread_incident_declarer.assert_called_once_with(runtime)

    def test_runtime_init_orders_thread_incident_declarer_after_incident_store(self) -> None:
        """The docstring invariant for declare_unresolvable_deadlock:
        ``set_thread_incident_declarer`` must run AFTER ``_incident_service`` is
        bound, because ``declare_unresolvable_deadlock`` dereferences
        ``self._incident_service``.

        Verify by checking ``_incident_service`` is non-None at the moment
        ``system.set_thread_incident_declarer`` was called (recorded via the
        mock's side_effect snapshot of the runtime state).
        """
        from unittest.mock import MagicMock

        from orca.runtime.system_runtime import SystemRuntime

        system = MagicMock()
        seen_incident_service: list[object] = []

        def _capture_state(declarer: object) -> None:
            # Snapshot _incident_service when set_thread_incident_declarer
            # fires; None/missing here means __init__ bound it too late.
            seen_incident_service.append(getattr(declarer, "_incident_service", None))

        system.set_thread_incident_declarer.side_effect = _capture_state

        SystemRuntime(system)

        assert len(seen_incident_service) == 1, (
            "set_thread_incident_declarer must be called exactly once during __init__"
        )
        assert seen_incident_service[0] is not None, (
            "_incident_service must be bound before set_thread_incident_declarer fires"
        )

    def test_runtime_implements_thread_incident_declarer_protocol(self) -> None:
        from orca.runtime.system_runtime import SystemRuntime

        assert hasattr(SystemRuntime, "declare_unresolvable_deadlock")
        assert callable(SystemRuntime.declare_unresolvable_deadlock)
        assert hasattr(SystemRuntime, "declare_action_failure")
        assert callable(SystemRuntime.declare_action_failure)


class TestExecutingThreadInvokesDeclarer:
    """The typed UnresolvableDeadlockError catch in ExecutingLabwareThread
    routes through ``_record_unresolvable_deadlock`` which calls the
    declarer with (execution_id, error.context).

    The catch's typed body is extracted into ``_record_unresolvable_deadlock``
    so these tests exercise the REAL production method rather than an
    inline copy. The catch in the action-resolution loop calls this
    same method -- a regression in the catch's invocation would be
    visible at the call site, not silently absorbed.
    """

    def _make_thread_with_declarer(
        self, declarer: IThreadIncidentDeclarer | None,
    ) -> ExecutingLabwareThread:
        """Construct an ExecutingLabwareThread bypassing __init__.

        Sets only the fields ``_record_unresolvable_deadlock`` touches:
        ``_thread_incident_declarer`` and ``_context`` (for execution_id).
        Keeps tests lean -- the real __init__ wires dozens of deps that
        are irrelevant to the record-incident contract.
        """
        from unittest.mock import MagicMock

        thread = ExecutingLabwareThread.__new__(ExecutingLabwareThread)
        thread._thread_incident_declarer = declarer
        thread._context = MagicMock()
        thread._context.execution_id = "exec-123"
        return thread

    def test_record_unresolvable_deadlock_invokes_declarer(self) -> None:
        ctx = UnresolvableDeadlockContext(
            requesting_thread_id="entry",
            requesting_labware_id="entry_lw",
            blocking_position_id="lh",
            blocking_thread_id="blocker",
            blocking_labware_id="blocker_lw",
            reason="blocking thread declared immovable=True",
            hint="hint",
        )
        error = UnresolvableDeadlockError(ctx)
        declarer = _FakeDeclarer()
        thread = self._make_thread_with_declarer(declarer)

        # Drive the REAL production method on ExecutingLabwareThread.
        thread._record_unresolvable_deadlock(error)

        assert declarer.calls == [("exec-123", ctx)]

    def test_record_unresolvable_deadlock_silent_when_declarer_none(self) -> None:
        """With ``_thread_incident_declarer = None`` (test fixtures that bypass
        SystemRuntime), the method returns silently. A separate
        ``_pause_for_error`` call in the production catch still fires
        -- but that path is not the responsibility of this method.
        """
        ctx = UnresolvableDeadlockContext(
            requesting_thread_id="entry",
            requesting_labware_id="entry_lw",
            blocking_position_id="lh",
            blocking_thread_id="blocker",
            blocking_labware_id="blocker_lw",
            reason="r",
            hint="h",
        )
        error = UnresolvableDeadlockError(ctx)
        # Verify against an explicit fake to assert "no calls happened".
        unused_fake = _FakeDeclarer()
        thread = self._make_thread_with_declarer(None)

        # No raise, no calls anywhere.
        result = thread._record_unresolvable_deadlock(error)

        assert result is None
        assert unused_fake.calls == [], (
            "no declarer means no calls anywhere (sanity)"
        )

    def test_record_unresolvable_deadlock_swallows_declarer_failure(self) -> None:
        """An incident-recording failure must NOT mask the underlying
        deadlock. The method swallows the exception (logged); the
        production catch then falls through to _pause_for_error so the
        operator still sees the typed error on the paused thread.
        """

        class _BrokenDeclarer(_FakeDeclarer):
            def declare_unresolvable_deadlock(
                self, execution_id: str, context: UnresolvableDeadlockContext,
            ) -> object:
                raise RuntimeError("incident store unavailable")

        ctx = UnresolvableDeadlockContext(
            requesting_thread_id="entry",
            requesting_labware_id="entry_lw",
            blocking_position_id="lh",
            blocking_thread_id="blocker",
            blocking_labware_id="blocker_lw",
            reason="r",
            hint="h",
        )
        error = UnresolvableDeadlockError(ctx)
        thread = self._make_thread_with_declarer(_BrokenDeclarer())

        # Returns normally despite the broken declarer.
        thread._record_unresolvable_deadlock(error)

    @pytest.mark.asyncio
    async def test_action_loop_catch_records_via_declarer(self) -> None:
        """Drive the REAL ``_run_method_loop`` so a deadlock raised by
        ``resolve_current_action`` flows through the typed
        ``except UnresolvableDeadlockError`` arm and reaches the declarer.

        The arm records the incident (declarer call) and then, on a non-RETRY
        decision at this pre-binding failure, tears the rendezvous down by
        raising ``_ThreadAbortedSignal`` (the operator's error rides in the
        recorded incident, not a re-raise -- a plain re-raise here left the
        paused thread stuck at PAUSED). Asserting both pins the connection: a
        future edit that drops ``_record_unresolvable_deadlock`` from the arm
        leaves the declarer un-called and fails this test.
        """
        from unittest.mock import AsyncMock, MagicMock

        from orca.workflow_models.labware_threads.executing_labware_thread import (
            ExecutingLabwareThread,
            _ThreadAbortedSignal,
        )
        from orca.workflow_models.status_enums import RecoveryDecision

        ctx = UnresolvableDeadlockContext(
            requesting_thread_id="entry",
            requesting_labware_id="entry_lw",
            blocking_position_id="lh",
            blocking_thread_id="blocker",
            blocking_labware_id="blocker_lw",
            reason="blocking thread declared immovable=True",
            hint="hint",
        )
        error = UnresolvableDeadlockError(ctx)
        declarer = _FakeDeclarer()

        thread = ExecutingLabwareThread.__new__(ExecutingLabwareThread)
        thread._thread_incident_declarer = declarer  # type: ignore[attr-defined]
        thread._context = MagicMock()  # type: ignore[attr-defined]
        thread._context.execution_id = "exec-123"  # type: ignore[attr-defined]
        thread._event_channel_registry = None  # type: ignore[attr-defined]
        thread._thread = MagicMock()  # type: ignore[attr-defined]
        thread._labware_location_service = MagicMock()  # type: ignore[attr-defined]
        thread._action_resolver = MagicMock()  # type: ignore[attr-defined]
        thread._holdover = MagicMock()  # type: ignore[attr-defined]
        thread._holdover.has_current.return_value = False
        thread._auto_spawn_with_recovery = AsyncMock()  # type: ignore[attr-defined]
        thread._fire = MagicMock()  # type: ignore[attr-defined]
        thread._check_abort_thread = MagicMock()  # type: ignore[attr-defined]
        thread._pause_for_error = AsyncMock(  # type: ignore[attr-defined]
            return_value=RecoveryDecision.ABORT_ACTION,
        )

        method = MagicMock()
        method.is_wait_step = False
        method.was_skipped = False
        method.was_aborted = False
        method.completed.is_set.return_value = False
        method.shared_coord.is_contributor.return_value = False
        method.consume_next_unresolved_action = AsyncMock(return_value=object())
        method.resolve_current_action = AsyncMock(side_effect=error)

        lane = MagicMock()
        lane.should_skip.return_value = False
        lane.next = AsyncMock(side_effect=[method, StopAsyncIteration()])
        thread._method_lane = lane  # type: ignore[attr-defined]

        with pytest.raises(_ThreadAbortedSignal):
            await thread._run_method_loop()

        assert declarer.calls == [("exec-123", ctx)]

