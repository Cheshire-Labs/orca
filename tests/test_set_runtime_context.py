"""Thread-side labware-registry registration is load-bearing.

The thread must appear in the per-execution `ILabwareRegistry` for
capacity / slot lookups to find it. `set_labware_registry` is the
one entry point that performs that registration; without it the
thread is invisible to those lookups.

(An earlier change widened this method into `set_runtime_context`
to plumb `labware_store` + `system` refs onto the thread for the
operator clear surface. Those refs turned out to be dead state --
written but never read -- and were removed; the registration
responsibility is back to `set_labware_registry`.)
"""

import asyncio
from typing import Any, Awaitable, Callable
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_state import (
    InMemoryLabwareRegistry, LabwareState,
)
from orca.runtime.recoverable_timeout import recoverable_timeout_coordinator
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)


def _make_thread(name: str = "t1") -> LabwareThreadInstance:
    labware = LabwareInstance("plate_x", "96_well")
    thread = MagicMock(spec=LabwareThreadInstance)
    thread.id = "tid-1"
    thread.name = name
    thread.labware = labware
    thread.labware_template = MagicMock()
    thread.labware_template.name = "plate_x"
    thread.start_location = MagicMock()
    thread.end_locations = [MagicMock()]
    thread.thread_template = None
    return thread


def _make_executing(thread: LabwareThreadInstance) -> ExecutingLabwareThread:
    return ExecutingLabwareThread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        actions_resolver=MagicMock(),
        context=MagicMock(),
        labware_location_service=MagicMock(),
    )


class TestSetLabwareRegistry:

    def test_registers_thread_in_registry(self) -> None:
        thread = _make_thread()
        et = _make_executing(thread)
        registry = InMemoryLabwareRegistry()

        et.set_labware_registry(registry)

        state = registry.get_state(thread.labware.id)
        assert state == LabwareState.IN_JOURNEY
        entry = registry._entries[thread.labware.id]
        assert entry.template_name == "plate_x"
        assert entry.thread is et

    def test_falls_back_to_thread_name_when_no_template(self) -> None:
        thread = _make_thread()
        thread.labware_template = None
        et = _make_executing(thread)
        registry = InMemoryLabwareRegistry()

        et.set_labware_registry(registry)

        entry = registry._entries[thread.labware.id]
        assert entry.template_name == thread.name

    def test_set_runtime_context_method_removed(self) -> None:
        """`set_runtime_context` is gone; only `set_labware_registry` remains."""
        thread = _make_thread()
        et = _make_executing(thread)
        assert not hasattr(et, "set_runtime_context")


class _SentinelCoordinator:
    """A stand-in coordinator left in the ContextVar by a prior task. If
    ``start()`` only conditionally seeded the ContextVar, this stale value
    would leak into the new thread's dispatch."""

    async def run_with_timeout(
        self,
        execution_id: str,
        device_id: str,
        command: str,
        max_seconds: float,
        coro_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        return await coro_factory()  # pragma: no cover

    def extend(self, incident_id: str, additional_seconds: float) -> None: ...

    def abort(self, incident_id: str, operator: str, reason: str) -> None: ...

    def mark_complete(self, incident_id: str, operator: str, reason: str) -> None: ...


class _ShortCircuit(Exception):
    """Aborts ``start()`` right after the ContextVar seed so the heavy
    method-loop machinery never runs."""


class TestStartSeedsCoordinatorToNoneWithoutDeclarer:
    """``start()`` ALWAYS seeds ``recoverable_timeout_coordinator`` -- to None
    when there is no incident declarer -- so a reused task cannot inherit a
    stale coordinator from a prior thread."""

    @pytest.mark.asyncio
    async def test_no_declarer_seeds_none_over_stale_sentinel(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        thread = _make_thread()
        # `start()` reaches `initialize_labware()` only when status == CREATED.
        status_manager = MagicMock()
        status_manager.get_status.return_value = "CREATED"
        et = ExecutingLabwareThread(
            thread=thread,
            event_bus=MagicMock(),
            move_handler=MagicMock(),
            status_manager=status_manager,
            actions_resolver=MagicMock(),
            context=MagicMock(),
            labware_location_service=MagicMock(),
        )
        # The constructor defaults `thread_incident_declarer` to None; confirm
        # the seam under test is the declarer-is-None branch.
        assert et._thread_incident_declarer is None

        seen: list[object] = []

        async def _capture_and_abort() -> None:
            seen.append(recoverable_timeout_coordinator.get())
            raise _ShortCircuit

        monkeypatch.setattr(et, "initialize_labware", _capture_and_abort)

        async def _run() -> None:
            recoverable_timeout_coordinator.set(_SentinelCoordinator())
            with pytest.raises(_ShortCircuit):
                await et.start()

        await asyncio.create_task(_run())

        assert seen == [None]
