"""Review item L8: `_resolve_reuse_bind` must release `spawn_lock`
BEFORE the slow `await self._labware_store.register(fresh)` call.

On a DB-backed `ILabwareStore`, `register()` is a network round-trip.
Holding the per-Location spawn_lock across that I/O serializes every
spawn-bind dispatch on the location -- even ones for different threads
that would have bound to the freshly-claimed labware. The lock only
needs to span the read-check-claim window: once
`Location.initialize_labware(fresh)` writes `_labware = fresh`, the next
contender's `start_loc.labware is not None` check sees the new labware
and binds.

This test instruments a slow `ILabwareStore.register` and asserts the
lock is released the moment the slot claim is visible, BEFORE the I/O
completes.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.workflow_models.workflows.executing_workflow import (
    ExecutingWorkflow, ResolvedAcquisition,
)


class _BlockingStore(InMemoryLabwareStore):
    """Wraps InMemoryLabwareStore but blocks `register` on a gate event
    so the test can observe lock state in the middle of the I/O step."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.register_entered = asyncio.Event()

    async def register(
        self, instance: LabwareInstance, execution_id: str | None = None,
    ) -> None:
        self.register_entered.set()
        await self.gate.wait()
        await super().register(instance, execution_id)


def _make_labware_template(name: str) -> Any:
    """Stub template with a create_instance() that returns a fresh
    LabwareInstance carrying the template name."""
    template = MagicMock()
    template.name = name
    template.create_instance = AsyncMock(side_effect=lambda: LabwareInstance(name, "96_well"))
    return template


def _make_thread_template(template_name: str, start_loc: Location) -> Any:
    tt = MagicMock()
    tt.name = "reuse-thread"
    tt.start_reuse_existing = True
    tt.labware_template = _make_labware_template(template_name)
    tt.start_location = start_loc
    return tt


def _make_system_stub():
    system = MagicMock()
    system.labwares = []
    system.add_labware = lambda lw: system.labwares.append(lw)
    # Fresh binds reconcile the LH deck after registering; on a non-LH start
    # location it is a no-op, but the call is awaited so it must be async.
    system.reconcile_lh_deck_occupancy = AsyncMock()
    # Reuse-bind finishes through the placement chokepoint (bind_resident);
    # awaited, so it must be async on the stub.
    system.labware_placer = MagicMock(bind_resident=AsyncMock())
    # A real ledger: the bound labware gets its opening entry before
    # bind_resident projects it.
    system.labware_contents = LabwareContentsLedger(OpsHistory())
    return system


def _make_executing_workflow_stub(
    system: Any, store: InMemoryLabwareStore,
) -> ExecutingWorkflow:
    """Construct an ExecutingWorkflow with the minimum scaffolding the
    `_resolve_reuse_bind` method touches. We pull the method off the
    class and bind it to a SimpleNamespace-style holder so we don't have
    to spin up the full constructor."""

    class _Holder:
        pass

    holder = _Holder()
    holder._labware_store = store
    holder._system = system
    holder._workflow = MagicMock(id="exec-test")
    holder._resolve_reuse_bind = (
        ExecutingWorkflow._resolve_reuse_bind.__get__(holder, _Holder)
    )
    return holder


async def test_spawn_lock_released_before_store_register() -> None:
    """The key L8 invariant: when `_resolve_reuse_bind` is partway
    through its slow `register()` I/O, the spawn_lock must already be
    released so other dispatch paths aren't blocked."""
    store = _BlockingStore()
    system = _make_system_stub()
    pad = PlatePad("pad1")
    start_loc = Location("pad1", resource=pad)
    template = _make_thread_template("plate_x", start_loc)
    ew = _make_executing_workflow_stub(system, store)

    task = asyncio.create_task(ew._resolve_reuse_bind(template))
    # Wait until the slow register() has been entered. That guarantees
    # the slot claim happened AND the await on the store began.
    await store.register_entered.wait()

    # The invariant: by the time register() is in flight, the lock
    # MUST already be released (the L8 fix). Pre-fix code would still
    # hold the lock here.
    assert start_loc.spawn_lock.locked() is False, (
        "spawn_lock still held during slow store.register() -- L8 regressed"
    )
    # And the slot is visibly claimed.
    assert start_loc.labware is not None
    assert start_loc.labware.template_name == "plate_x"

    # Release the I/O and confirm the task completes cleanly.
    store.gate.set()
    resolved: ResolvedAcquisition = await task
    assert resolved.labware_instance is start_loc.labware
    assert resolved.created_fresh is True


async def test_lock_held_for_existing_match_branch() -> None:
    """Sanity: when the slot already holds a matching instance, the
    bind-and-return path stays INSIDE the lock (no I/O happens, so
    holding the lock costs nothing). This pins that the L8 restructure
    didn't accidentally widen the lock-release to the bind branch too."""
    store = _BlockingStore()
    system = _make_system_stub()
    pad = PlatePad("pad1")
    start_loc = Location("pad1", resource=pad)

    existing = LabwareInstance("plate_x", "96_well")
    start_loc.initialize_labware(existing)

    template = _make_thread_template("plate_x", start_loc)
    ew = _make_executing_workflow_stub(system, store)

    # The bind branch returns synchronously without calling store.register.
    resolved = await ew._resolve_reuse_bind(template)

    assert resolved.labware_instance is existing
    assert resolved.created_fresh is False
    # Lock released after the bind returned.
    assert start_loc.spawn_lock.locked() is False
    # No I/O happened on the bind path.
    assert store.register_entered.is_set() is False
