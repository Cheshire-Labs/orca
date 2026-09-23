"""Reuse-bind anchors a resident's identity to its physical container.

A deck-resident reagent the operator refills (not replaces) must keep ONE stable
labware id across executions and restarts. Reuse-bind consults the durable store
by exact position and rebinds to the persisted instance instead of minting a
fresh id when the in-memory slot is empty -- the two-ids-one-container wedge that
made a stale seed collide with a fresh one.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.resource_models.deck_site import DeckSite
from orca.state.current import placement_ledger
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.runtime_interface import (
    SpawnIncompatibleError,
)
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow

RESIDENT_POSITION = "lh/carrier-25-0"


def _deck_site_start() -> Location:
    # Flat model: a deck site is a top-level node with no parent.
    return Location(RESIDENT_POSITION, resource=DeckSite(RESIDENT_POSITION))


def _reuse_template(start_location: Location, template_name: str = "reservoir") -> MagicMock:
    template = MagicMock()
    template.start_reuse_existing = True
    template.name = "reservoir_journey"
    template.start_location = start_location
    template.labware_template.name = template_name
    template.labware_template.create_instance = AsyncMock(
        side_effect=lambda: LabwareInstance(template_name, "trough")
    )
    return template


def _executing_workflow(store: InMemoryLabwareStore) -> ExecutingWorkflow:
    ew = object.__new__(ExecutingWorkflow)
    ew._labware_store = store
    system = MagicMock()
    system.reconcile_lh_deck_occupancy = AsyncMock()
    # Reuse-bind finishes through the placement chokepoint (bind_resident).
    system.labware_placer = MagicMock(bind_resident=AsyncMock())
    # A real ledger: the bound labware must get its opening entry BEFORE
    # bind_resident projects it, or the driver keeps its own default.
    system.labware_contents = LabwareContentsLedger(OpsHistory())
    ew._system = system
    ew._workflow = MagicMock(id="exec-test")
    return ew


async def _persist_resident(
    store: InMemoryLabwareStore, position: str, template_name: str = "reservoir",
) -> LabwareInstance:
    instance = LabwareInstance(template_name, "trough")
    await store.register(instance)
    await store.update_location(instance.id, position)
    return instance


class TestStorePositionLookup:
    async def test_get_by_position_returns_instance_at_position(self) -> None:
        store = InMemoryLabwareStore()
        instance = await _persist_resident(store, RESIDENT_POSITION)
        found = await store.get_by_position(RESIDENT_POSITION)
        assert found is instance

    async def test_get_by_position_unknown_is_none(self) -> None:
        store = InMemoryLabwareStore()
        assert await store.get_by_position("nowhere") is None

    async def test_get_by_position_newest_registered_wins(self) -> None:
        store = InMemoryLabwareStore()
        older = LabwareInstance("reservoir", "trough")
        await store.register(older)
        await store.update_location(older.id, RESIDENT_POSITION)
        newer = LabwareInstance("reservoir", "trough")
        await store.register(newer)
        await store.update_location(newer.id, RESIDENT_POSITION)

        found = await store.get_by_position(RESIDENT_POSITION)
        assert found is not None and found.id == newer.id


class TestReuseBindIdentity:
    async def test_rebinds_to_persisted_resident_keeping_id(self) -> None:
        store = InMemoryLabwareStore()
        persisted = await _persist_resident(store, RESIDENT_POSITION)
        start = _deck_site_start()
        ew = _executing_workflow(store)

        resolved = await ew._resolve_reuse_bind(_reuse_template(start))

        assert resolved is not None
        assert resolved.labware_instance.id == persisted.id
        assert resolved.created_fresh is False
        assert start.labware is persisted

    async def test_mints_fresh_when_no_persisted_resident(self) -> None:
        store = InMemoryLabwareStore()
        start = _deck_site_start()
        ew = _executing_workflow(store)

        resolved = await ew._resolve_reuse_bind(_reuse_template(start))

        assert resolved is not None
        assert resolved.created_fresh is True
        assert await store.get_by_id(resolved.labware_instance.id) is not None

    async def test_a_handoff_that_was_not_spent_still_adopts_the_resident(self) -> None:
        """Only used-up labware makes a replacement ask for a fresh one.

        A capacity-full handoff, or a closed slot taking a late contribution,
        leaves a perfectly good resident standing where it was.
        """
        store = InMemoryLabwareStore()
        persisted = await _persist_resident(store, RESIDENT_POSITION)
        start = _deck_site_start()
        ew = _executing_workflow(store)

        resolved = await ew._resolve_reuse_bind(
            _reuse_template(start), replaces_spent_labware=False,
        )

        assert resolved is not None
        assert resolved.labware_instance.id == persisted.id

    async def test_a_replacement_for_spent_labware_does_not_adopt_it(self) -> None:
        store = InMemoryLabwareStore()
        await _persist_resident(store, RESIDENT_POSITION)
        start = _deck_site_start()
        ew = _executing_workflow(store)

        resolved = await ew._resolve_reuse_bind(
            _reuse_template(start), replaces_spent_labware=True,
        )

        assert resolved is None, (
            "the replacement must fall through to its declared spawn action, "
            "not adopt the labware that just ran out"
        )

    async def test_stable_id_across_two_binds_after_slot_drift(self) -> None:
        store = InMemoryLabwareStore()
        start = _deck_site_start()
        ew = _executing_workflow(store)

        first = await ew._resolve_reuse_bind(_reuse_template(start))
        assert first is not None and first.created_fresh is True
        # The fresh bind persisted the resident's position itself (no manual
        # sync). Simulate the in-memory slot emptying (post-rehydrate / cross-execution).
        assert await store.get_by_position(RESIDENT_POSITION) is not None
        # One record, so emptying the slot means vacating it. Swapping the
        # holder object no longer hides a plate the ledger still holds.
        start.resource = DeckSite(RESIDENT_POSITION)
        placement_ledger().vacate(first.labware_instance.ref)
        assert start.labware is None

        second = await ew._resolve_reuse_bind(_reuse_template(start))

        assert second is not None
        assert second.labware_instance.id == first.labware_instance.id
        assert second.created_fresh is False

    async def test_fresh_resident_is_registered_with_execution_id(self) -> None:
        store = InMemoryLabwareStore()
        start = _deck_site_start()
        ew = _executing_workflow(store)
        captured: list[str | None] = []
        original = store.register

        async def _spy(instance: LabwareInstance, execution_id: str | None = None) -> None:
            captured.append(execution_id)
            await original(instance, execution_id)

        setattr(store, "register", _spy)
        await ew._resolve_reuse_bind(_reuse_template(start))
        assert captured == ["exec-test"]

    async def test_wrong_template_at_position_raises_incompatible(self) -> None:
        store = InMemoryLabwareStore()
        await _persist_resident(store, RESIDENT_POSITION, template_name="other_reagent")
        start = _deck_site_start()
        ew = _executing_workflow(store)

        with pytest.raises(SpawnIncompatibleError):
            await ew._resolve_reuse_bind(_reuse_template(start, template_name="reservoir"))


