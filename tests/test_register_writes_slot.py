"""Silent-stall root cause: ``LabwareFacade.register(location=X)`` must
write ``X.labware`` so the pre-submission start_location check sees the
operator-placed labware.

Before this fix, ``register(location=X)`` updated the location-history
tracker and the identity store but left ``X._labware = None``. An operator
who physically placed a plate, called register, and then submitted a
workflow with ``start=X`` would trip the canonical silent stall: the
pre-check saw an empty slot, accepted the submission, the thread started,
the engine fabricated a brand-new ``LabwareInstance`` of the same template,
and wrote it on top of the operator's plate. Two conceptual plates,
one physical plate; the transporter picks the engine's fabricated plate
and orphans the operator's. The previous retry-forever loop in
``initialize_labware`` masked the engine-side write conflict; the silent
stall the operator saw was a downstream effect.

The fix is a one-liner inside ``LabwareFacade.register``: when ``location``
is supplied, call ``target_location.initialize_labware(instance)`` and fire
``notify_initialized`` so observers (transporter ``ensure_seeded``) see
the new state. If the slot is occupied, ``SlotOccupiedError`` raises
before any registry state changes -- the operator gets a 409 naming the
occupant instead of a silent stall.

This module pins:
- The slot is written
- Observers fan-out
- A subsequent submit at the same start_location trips the start_location pre-check
- ``register`` without a location is unchanged
- An occupied slot raises before any partial registration
- The clear surfaces (``discharge``, ``clear_all``) still clear after register
- The location-history tracker is still updated (no regression on the
  existing behavior)
- IPlateSource locations (stackers, hotels) go through the bridge's
  staged_labware writer the same way
"""

import pytest

from orca.devices.devices import Storage
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.runtime_interface import StartLocationsOccupiedError
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_system_runtime import _build_simple_system


class TestRegisterWithLocationWritesSlot:
    """Positive case: register(location=X) -> X.labware is the new instance."""

    async def test_register_with_location_writes_target_slot(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            pad1 = system.system_map.get_location("pad1")
            assert pad1.labware is None  # baseline

            snap = await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )

            assert pad1.labware is not None, (
                "register(location=pad1) must populate pad1.labware -- "
                "without this write, the start_location pre-check silently accepts "
                "submissions that overwrite the operator's plate"
            )
            assert pad1.labware.id == snap.id
        finally:
            await runtime.shutdown()

    async def test_register_with_location_fires_initialized_observers(
        self,
    ) -> None:
        """The slot write must go through the location's notify_initialized
        path so transporters fire ``ensure_seeded`` and stay in sync with the
        new labware."""
        from orca.resource_models.labware import LabwareInstance as LI
        from orca.resource_models.location import (
            ILabwareLocationObserver,
            Location,
        )

        seen_events: list[tuple[str, str]] = []

        class _Observer(ILabwareLocationObserver):
            async def notify_labware_location_change(
                self, event: str, location: Location, labware: LI,
            ) -> None:
                seen_events.append((event, labware.template_name))

        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            pad1 = system.system_map.get_location("pad1")
            pad1.add_observer(_Observer())

            await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )

            assert ("initialized", "plate_96") in seen_events, (
                f"register(location) must fire 'initialized' so observers "
                f"sync. Seen: {seen_events}"
            )
        finally:
            await runtime.shutdown()

    async def test_register_with_location_preserves_history_tracker(
        self,
    ) -> None:
        """The existing tracker update path is preserved (no regression)."""
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            # The labware-history tracker is exposed via
            # ``LabwareFacade.get_history(labware_id)``; the last event in
            # the history reflects the most recent location update. We
            # assert via the public facade method rather than reaching into
            # the private ``_location_service`` to keep the test stable as
            # the internal layout evolves.
            history = await runtime.labware.get_history(snap.id)
            assert history, (
                "register(location) must update the labware-history tracker; "
                "get_history returned empty"
            )
            assert history[-1].position_id == "pad1", (
                f"register(location=pad1) must record a tracker event at "
                f"pad1; last event was at {history[-1].position_id!r}"
            )
        finally:
            await runtime.shutdown()


class TestRegisterRefusesIfSlotOccupied:
    """Operator-protection: register-with-location must fail before any
    partial state change if the target slot already holds labware."""

    async def test_register_at_occupied_pad_raises_slot_occupied(self) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            pad1 = system.system_map.get_location("pad1")
            existing = LabwareInstance("plate_96", "96_well")
            system.add_labware(existing)
            pad1.initialize_labware(existing)
            assert pad1.labware is existing

            initial_labware_count = len(list(system.labwares))

            # The Operation above only converts SlotOccupiedError; a bare
            # DeviceBusyError from this chain reaches the wire as a 500.
            with pytest.raises(SlotOccupiedError):
                await runtime.labware.register(
                    "plate_96", location="pad1",
                    confirm=True,
                )

            # No partial registration: system.labwares unchanged.
            assert len(list(system.labwares)) == initial_labware_count, (
                "register raised mid-flight; no new labware should be "
                "registered. Partial-registration leaks operator state."
            )
            # And the existing labware is still the one at pad1.
            assert pad1.labware is existing
        finally:
            await runtime.shutdown()


class TestRegisterWithoutLocationDoesNotWriteSlot:
    """register without location is a pure identity-store + system-registry
    operation; no Location slot should be touched."""

    async def test_register_without_location_leaves_all_slots_empty(
        self,
    ) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            await runtime.labware.register(
                "plate_96", barcode="BC-1", confirm=True,
            )

            for loc_name in ("pad1", "pad2"):
                loc = system.system_map.get_location(loc_name)
                assert loc.labware is None, (
                    f"register without location must not touch any slot; "
                    f"{loc_name} has labware after register"
                )
        finally:
            await runtime.shutdown()


class TestSilentStallReproduction:
    """The canonical silent stall scenario, end-to-end.

    Before this fix: operator registers labware at pad1, then submits a
    workflow with start=pad1. The pre-check sees pad1.labware is None
    (because register-bypass), accepts. The thread starts and fabricates
    a phantom plate over the operator's. Engine and operator state diverge
    silently.

    After this fix: register writes pad1.labware. The pre-check sees pad1
    occupied and refuses with StartLocationsOccupiedError. Operator gets
    a typed error naming the location they need to clear.
    """

    async def test_register_then_submit_same_start_trips_precheck(
        self,
    ) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            # Operator places labware and registers it.
            await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )

            # Same workflow that uses pad1 as start. Before register wrote the
            # slot, this is where the run stalled in silence:
            # this submit was accepted, the thread overwrote the operator's
            # plate, no error was raised.
            with pytest.raises(StartLocationsOccupiedError) as exc_info:
                await runtime.submit_workflow("simple_workflow", mode=WorkflowRunMode.PURE_SIM)
            err = exc_info.value
            assert any(
                slot.position_id == "pad1" for slot in err.occupied
            ), (
                f"StartLocationsOccupiedError must name pad1 as the "
                f"occupied slot; got {err.occupied}"
            )
        finally:
            await runtime.shutdown()

    async def test_register_then_discharge_clears_slot_so_submit_succeeds(
        self,
    ) -> None:
        """Recovery path: after operator clears via discharge, submit
        succeeds normally. Pins the full register -> stall-refuse ->
        discharge -> retry-submit lifecycle."""
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            # First submit refuses (the stall protection).
            with pytest.raises(StartLocationsOccupiedError):
                await runtime.submit_workflow("simple_workflow", mode=WorkflowRunMode.PURE_SIM)

            # Operator physically picks the plate off the deck and informs
            # the engine via discharge.
            await runtime.labware.discharge_labware(snap.id, force=True)
            pad1 = system.system_map.get_location("pad1")
            assert pad1.labware is None

            # Retry-submit now succeeds.
            sub = await runtime.submit_workflow("simple_workflow", mode=WorkflowRunMode.PURE_SIM)
            assert sub is not None
        finally:
            await runtime.shutdown()


class TestRegisterClearToolIntegration:
    """The clear surfaces (discharge, clear_all) operate on registered
    labware regardless of whether it has a slot write."""

    async def test_discharge_after_register_with_location_clears_slot(
        self,
    ) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            pad1 = system.system_map.get_location("pad1")
            assert pad1.labware is not None

            await runtime.labware.discharge_labware(snap.id, force=True)
            assert pad1.labware is None
        finally:
            await runtime.shutdown()

    async def test_clear_all_after_register_with_location_clears_slot(
        self,
    ) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            snap = await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            pad1 = system.system_map.get_location("pad1")
            assert pad1.labware is not None

            cleared = await runtime.labware.clear_all_labware(force=True)
            assert snap.id in cleared
            assert pad1.labware is None
        finally:
            await runtime.shutdown()


class TestRegisterAtIPlateSource:
    """IPlateSource-backed locations (stackers, hotels) take the same write
    path; the bridge's staged_labware is the analog of PlatePad._labware
    for the OUTPUT position."""

    async def test_register_at_storage_writes_bridge_staged_labware(
        self,
    ) -> None:
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            pad1 = system.system_map.get_location("pad1")
            storage = Storage("test_stacker")
            bridge = LabwareStagingBridge("pad1", storage)
            pad1._resource = bridge
            assert pad1.labware is None

            snap = await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )

            assert pad1.labware is not None
            assert pad1.labware.id == snap.id
        finally:
            await runtime.shutdown()

    async def test_register_at_storage_does_not_trip_iplate_source_precheck(
        self,
    ) -> None:
        """The IPlateSource skip in the pre-check still applies: the
        source's output being occupied does NOT block submission (the
        source has more plates queued behind). The slot
        write from register is bookkeeping for the operator-visible state;
        the pre-check semantic for plate sources is unchanged.
        """
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            pad1 = system.system_map.get_location("pad1")
            storage = Storage("test_stacker")
            bridge = LabwareStagingBridge("pad1", storage)
            pad1._resource = bridge

            await runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            assert pad1.labware is not None

            # Storage-backed start_location with output occupied: pre-check
            # skips and accepts.
            sub = await runtime.submit_workflow("simple_workflow", mode=WorkflowRunMode.PURE_SIM)
            assert sub is not None
        finally:
            await runtime.shutdown()
