"""A deck-layout change that removes an occupied site is a conflict, not a
silent drop.

The gap this pins (session-rebuild reconcile handoff): the occupancy reconcile
skipped any ledger occupant whose site the new deck config no longer provides,
so the labware quietly vanished from the driver while the world model still
claimed the site. The operator was told nothing until a later command failed
with a resource-not-found that read like the labware never existed. The record
must survive and a human must be told: the reconcile now reports the conflict,
and the runtime records a DECK_RECONCILE_CONFLICT incident naming the labware
and the vanished site.
"""

from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from orca.system.system_interface import DeckReconcileConflict
from tests.test_labware_state_reconciliation import (
    DECK_CONFIG,
    RESERVOIR_SITE,
    _build,
)

CONFIG_WITHOUT_RESERVOIR_CARRIER = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
    ],
)


async def _runtime_with_resident():
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, lh = await _build(recorder, wf_name="deck_conflict")
    runtime = SystemRuntime(build.system, event_bus=build.event_bus,
                            labware_store=InMemoryLabwareStore())
    await runtime.start()
    snap = await runtime.labware.register(
        "reservoir", location=RESERVOIR_SITE, confirm=True,
    )
    return build, lh, runtime, snap


async def test_vanished_site_reports_a_conflict() -> None:
    build, lh, runtime, snap = await _runtime_with_resident()
    try:
        conflicts: list[DeckReconcileConflict] = []
        build.system.add_deck_reconcile_conflict_listener(conflicts.append)

        await lh.deck_layout_store.update(
            "default", CONFIG_WITHOUT_RESERVOIR_CARRIER,
        )
        lh_location = build.system.system_map.get_resource_location(lh.name)
        await build.system.reconcile_lh_deck_occupancy(lh_location)

        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert conflict.labware_id == snap.id
        assert conflict.position_id == RESERVOIR_SITE
        assert conflict.device_name == lh.name
    finally:
        await runtime.shutdown()


async def test_runtime_records_the_conflict_as_an_incident() -> None:
    build, lh, runtime, snap = await _runtime_with_resident()
    try:
        await lh.deck_layout_store.update(
            "default", CONFIG_WITHOUT_RESERVOIR_CARRIER,
        )
        lh_location = build.system.system_map.get_resource_location(lh.name)
        await build.system.reconcile_lh_deck_occupancy(lh_location)

        incidents = await runtime.incidents.list(
            category=IncidentCategory.DECK_RECONCILE_CONFLICT,
        )
        assert len(incidents) == 1
        assert snap.name in incidents[0].message
        assert RESERVOIR_SITE in incidents[0].message
    finally:
        await runtime.shutdown()


async def test_conflict_does_not_drop_the_record_or_block_other_sites(
) -> None:
    """The orphan stays in the world model (a conflict is not a retraction)
    and the rest of the deck still projects."""
    build, lh, runtime, snap = await _runtime_with_resident()
    try:
        await lh.deck_layout_store.update(
            "default", CONFIG_WITHOUT_RESERVOIR_CARRIER,
        )
        lh_location = build.system.system_map.get_resource_location(lh.name)
        await build.system.reconcile_lh_deck_occupancy(lh_location)

        refreshed = await runtime.labware.get_by_id(snap.id)
        assert refreshed.current_location == RESERVOIR_SITE
    finally:
        await runtime.shutdown()


async def test_unchanged_layout_reports_no_conflict() -> None:
    build, lh, runtime, _snap = await _runtime_with_resident()
    try:
        conflicts: list[DeckReconcileConflict] = []
        build.system.add_deck_reconcile_conflict_listener(conflicts.append)

        assert (await lh.deck_layout_store.get("default")) == DECK_CONFIG
        lh_location = build.system.system_map.get_resource_location(lh.name)
        await build.system.reconcile_lh_deck_occupancy(lh_location)

        assert conflicts == []
    finally:
        await runtime.shutdown()
