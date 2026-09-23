"""Reconciliation invariants: every holder of "where is this labware" must
agree after each operation that touches labware position.

The labware single-source-of-truth model spreads position across
several holders that MUST stay reconciled:

  - the labware store (authority of record across reboots)
  - the engine ledger: ``ILabwareLocationService`` (instance->Location index)
    AND every ``Location.labware`` slot, including deck-site child slots
  - ``system.labwares`` (the engine's identity registry)
  - device projections: the transporter world graph and the LH driver deck

The wedge that motivated this work was these holders drifting apart. These
tests drive the runtime through bind / clear-all / reboot and assert the
holders converge, so a future change that desyncs one of them fails loudly.
"""


import pytest

import orca.orca as orca
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest
from orca.devices.devices import DeckLabwareIdentityError, LiquidHandler, Storage
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.resource_models.labware import LabwareInstance, PlateInstance
from orca.runtime.sim_labware import SimPlate
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.interfaces import ILabwareStore
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import named_for_template, run_to_quiescence


RESERVOIR_SITE = "lh/carrier-25-0"

DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)


async def _build(
    recorder: RecordingLiquidHandlerDriver,
    *,
    wf_name: str,
    sim_recorder: RecordingLiquidHandlerDriver | None = None,
):
    """A sample plate that joins a resident reservoir pinned to a deck site.

    Running the workflow binds the reservoir at ``RESERVOIR_SITE`` exactly
    once, so the holders should all agree it lives there afterward.

    ``sim_recorder`` gives the device a separate sim-world driver, so a test
    can tell which world a call landed in. Left out, one recorder answers for
    both and world is invisible.
    """
    stores = InMemoryRuntimeStoreFactory()

    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")

    class _LhDeckFactory:
        def __init__(
            self,
            lh: RecordingLiquidHandlerDriver,
            sim: RecordingLiquidHandlerDriver,
        ) -> None:
            self._lh = lh
            self._sim = sim
            from orca.runtime.device_factory import SimDeviceFactory
            self._fallback = SimDeviceFactory()

        def build_drivers(self, device_type: str, name: str, *, deck_modeling: bool = False):
            if device_type == "liquid_handler":
                return self._lh, self._sim
            return self._fallback.build_drivers(device_type, name, deck_modeling=deck_modeling)

    with use_device_factory(_LhDeckFactory(recorder, sim_recorder or recorder)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG}),
            deck_layout="default",
        )
        stacker = Storage("stacker")
        waste = Storage("waste")
        pad = PlatePad("pad")
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("pad", C(200, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("lh/carrier-7-2", C(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(
        device=lh,
        inputs=[sample_plate, reservoir],
        deck_positions={sample_plate: "carrier-7-0"},
    )
    async def add_reagent(ctx: ActionContext) -> None:
        ctx.labware("reservoir")

    @orca.method
    async def add_reagent_method(ctx: MethodContext):
        yield add_reagent

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield add_reagent_method

    @orca.thread(
        labware=reservoir,
        start=(RESERVOIR_SITE, REUSE_EXISTING),
        end=(RESERVOIR_SITE, LEAVE_IN_PLACE),
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add_reagent_method])

    @orca.workflow(name=wf_name)
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Reconciliation", workflow=workflow, topology=topology, stores=stores)
    return build, lh


def _resident_at(build, site: str = RESERVOIR_SITE) -> LabwareInstance | None:
    return build.system.system_map.get_location(site).labware


def _require_resident(build, site: str = RESERVOIR_SITE) -> LabwareInstance:
    resident = _resident_at(build, site)
    assert resident is not None, f"expected a resident at {site}, found none"
    return resident


async def _store_position(store: ILabwareStore, site: str = RESERVOIR_SITE) -> str | None:
    for labware_id, position_id in await store.list_active_locations():
        if position_id == site:
            return labware_id
    return None


async def _deck_labware_names(lh) -> set[str]:
    deck = await lh.driver.get_deck_state(GetDeckStateRequest())
    return {item.name for item in deck.labware}


async def _bind_resident(recorder, store: ILabwareStore, *, wf_name: str):
    """Build a runtime over ``store``, run once, return (build, lh, runtime)."""
    build, lh = await _build(recorder, wf_name=wf_name)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus, labware_store=store)
    await runtime.start()
    record = await runtime.submit_workflow(wf_name, mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, record.id)
    assert statuses and all(s == "COMPLETED" for s in statuses.values()), statuses
    return build, lh, runtime


@pytest.mark.slow
@pytest.mark.asyncio
async def test_bind_reconciles_every_holder() -> None:
    """After a resident binds, store / ledger slot / registry / driver deck all
    name the same one instance at the same site."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = InMemoryLabwareStore()
    build, lh, _ = await _bind_resident(recorder, store, wf_name="recon_bind")

    slot = _resident_at(build)
    assert slot is not None, "ledger slot at the deck site is empty after bind"
    assert slot.template_name == "reservoir"

    assert await _store_position(store) == slot.id, (
        "store position of record disagrees with the ledger slot id"
    )
    assert slot.id in {lw.id for lw in build.system.labwares}, (
        "engine registry does not know the bound resident"
    )
    assert any(named_for_template(n, "reservoir") for n in await _deck_labware_names(lh)), (
        "driver deck projection missing the resident"
    )
    assert build.system.labware_location_service.get(slot).position_id == RESERVOIR_SITE, (
        "ledger service tracks the resident at the wrong site"
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_clear_all_wipes_every_holder() -> None:
    """clear-all is the complete one-button recovery: the store, the ledger
    service, every Location slot (incl. the deck-site child), the registry, and
    the driver deck occupancy all come back empty; carriers are preserved. The
    sixth holder (the transporter world projection) is reset via the same
    clear_all path and is guarded directly by
    test_labware_clear_tools.test_resets_transporter_world_projection."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = InMemoryLabwareStore()
    build, lh, runtime = await _bind_resident(recorder, store, wf_name="recon_clear")
    # Preconditions: prove each holder is non-empty BEFORE the clear, so the
    # post-clear emptiness can't pass for the wrong reason.
    assert _resident_at(build) is not None, "resident never bound to its deck site"
    assert any(named_for_template(n, "reservoir") for n in await _deck_labware_names(lh)), "resident never reached the driver deck"

    await runtime.labware.clear_all_labware(force=True)

    assert _resident_at(build) is None, (
        "deck-site Location slot still holds the cleared resident"
    )
    assert await store.list_active_locations() == [], "store still has positions"
    assert list(build.system.labwares) == [], "registry still has labware"
    assert build.system.labware_location_service.get_all() == {}, "ledger service not empty"
    deck_names = await _deck_labware_names(lh)
    assert not any(named_for_template(n, "reservoir") for n in deck_names), "driver deck still occupied by the resident"
    # Carriers survive a labware clear (STRUCTURE preserved).
    assert "carrier-25" in deck_names, "clear wiped the carriers"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_reboot_without_clear_rehydrates_same_resident() -> None:
    """Persist-until-explicit-clear: a fresh runtime over the same store
    rehydrates the resident to the same id, registers it, and reuses (not
    respawns) it on the next run."""
    recorder1 = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = InMemoryLabwareStore()
    build1, _, _ = await _bind_resident(recorder1, store, wf_name="recon_reboot_keep")
    id1 = _require_resident(build1).id

    recorder2 = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build2, lh2 = await _build(recorder2, wf_name="recon_reboot_keep_2")
    runtime2 = SystemRuntime(
        build2.system, event_bus=build2.event_bus, labware_store=store)
    await runtime2.start()

    rehydrated = _resident_at(build2)
    assert rehydrated is not None and rehydrated.id == id1, (
        "reboot did not rehydrate the persisted resident to its stable id"
    )
    assert id1 in {lw.id for lw in build2.system.labwares}, (
        "rehydrated resident missing from the engine registry"
    )

    record = await runtime2.submit_workflow("recon_reboot_keep_2", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime2, record.id)
    assert all(s == "COMPLETED" for s in statuses.values()), statuses
    assert _require_resident(build2).id == id1, "resident was respawned across reboot, not reused"
    assert any(named_for_template(n, "reservoir") for n in await _deck_labware_names(lh2)), "driver deck missing rehydrated resident"


class _IdentityOnlyStore(InMemoryLabwareStore):
    """A store that hands identity back and nothing else, like a real database.

    The in-memory store keeps the original instance object, so a reboot through
    it silently returns a labware that still carries its PLR object. A row in a
    database cannot, and rebuilding one from the row is what a hosted deployment
    actually does on the next boot.
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_reboot_through_an_identity_only_store_restores_the_resident() -> None:
    """The bench wedge: a resident persisted in a database comes back knowing
    only its identity, and the deck reconciliation on the next build reads the
    PLR object off every resident. The runtime rebuilds it from its template
    first, so the reboot resumes instead of taking the deployment to 503.
    """
    recorder1 = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = _IdentityOnlyStore()
    build1, _, _ = await _bind_resident(recorder1, store, wf_name="recon_identity_reboot")
    id1 = _require_resident(build1).id

    recorder2 = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build2, lh2 = await _build(recorder2, wf_name="recon_identity_reboot_2")
    runtime2 = SystemRuntime(
        build2.system, event_bus=build2.event_bus, labware_store=store)
    await runtime2.start()

    rehydrated = _require_resident(build2)
    assert rehydrated.id == id1, "reboot lost the persisted resident id"
    assert isinstance(rehydrated, PlateInstance), (
        "resident came back identity-only; the deck cannot read its wells and "
        "the driver falls back to its own defaults"
    )

    record = await runtime2.submit_workflow(
        "recon_identity_reboot_2", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime2, record.id)
    assert all(s == "COMPLETED" for s in statuses.values()), statuses
    assert any(named_for_template(n, "reservoir") for n in await _deck_labware_names(lh2)), (
        "driver deck missing the restored resident"
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_reboot_after_clear_does_not_resurrect() -> None:
    """A cleared resident stays gone across a reboot: the fresh runtime
    rehydrates nothing, and a later run creates a NEW instance (the cleared
    id is gone for good)."""
    recorder1 = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = InMemoryLabwareStore()
    build1, _, runtime1 = await _bind_resident(recorder1, store, wf_name="recon_reboot_clear")
    id1 = _require_resident(build1).id
    await runtime1.labware.clear_all_labware(force=True)

    recorder2 = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build2, _ = await _build(recorder2, wf_name="recon_reboot_clear_2")
    runtime2 = SystemRuntime(
        build2.system, event_bus=build2.event_bus, labware_store=store)
    await runtime2.start()

    assert _resident_at(build2) is None, "cleared resident resurrected on reboot"
    assert list(build2.system.labwares) == [], "cleared resident reappeared in the registry"
    assert await store.list_active_locations() == [], "clear did not delete the store record"

    record = await runtime2.submit_workflow("recon_reboot_clear_2", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime2, record.id)
    assert all(s == "COMPLETED" for s in statuses.values()), statuses
    new_id = _require_resident(build2).id
    assert new_id != id1, "a run after clear+reboot resurrected the cleared instance id"
    # Fresh resident minted + persisted under a new id (the load-bearing
    # non-resurrection proof is the empty store/slot/registry asserted pre-run).
    assert await _store_position(store) == new_id, (
        "the post-clear resident was not freshly persisted under its new id"
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_discharge_clears_deck_site_resident_slot() -> None:
    """discharge_labware shares the same _wipe_labware/_all_locations path as
    clear-all, so it too must reach a resident's deck-site CHILD slot, not just
    registry + store. Guards the fix for the single-instance clear surface (a
    revert of _all_locations would ship green through clear-all alone)."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = InMemoryLabwareStore()
    build, _, runtime = await _bind_resident(recorder, store, wf_name="recon_discharge")
    resident_id = _require_resident(build).id

    await runtime.labware.discharge_labware(resident_id, force=True)

    assert _resident_at(build) is None, "discharge left the deck-site Location slot stale"
    assert resident_id not in {lw.id for lw in build.system.labwares}, "still in registry"
    assert await _store_position(store) is None, "still in store"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_two_same_template_residents_both_place() -> None:
    """Deck resources are named by INSTANCE, so two residents of one template
    on one device are both representable: same-template coexistence is
    operator-managed layout, not an engine refusal. Reconcile
    places both, each under its own unique name."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = InMemoryLabwareStore()
    build, lh, _ = await _bind_resident(recorder, store, wf_name="recon_collide")

    # Seed a SECOND "reservoir" resident on a different deck site of the same LH.
    second_site = build.system.system_map.get_location("lh/carrier-25-1")
    second = await build.system.get_labware_template("reservoir").create_instance()
    await second.enter_record(build.system.labware_contents)
    second_site.initialize_labware(second)
    build.system.add_labware(second)

    lh_location = build.system.system_map.get_location("lh")
    await build.system.reconcile_lh_deck_occupancy(lh_location)

    deck = await lh.driver.get_deck_state(GetDeckStateRequest())
    reservoirs = [
        item.name for item in deck.labware
        if named_for_template(item.name, "reservoir")
    ]
    assert len(reservoirs) == 2 and len(set(reservoirs)) == 2, (
        f"both same-template residents must hold distinct deck resources; "
        f"deck held {reservoirs}"
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_one_name_at_two_sites_raises_ledger_corruption() -> None:
    """Instance names are unique by mint, so one name occupying two deck sites
    can only mean a corrupted ledger; reconcile refuses loudly instead of
    silently projecting one of the two."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    store = InMemoryLabwareStore()
    build, _, _ = await _bind_resident(recorder, store, wf_name="recon_dup_name")

    resident = _require_resident(build)
    clone = PlateInstance(SimPlate(resident.name), template_name=resident.template_name, labware_type=resident.labware_type)
    assert clone.name == resident.name and clone.id != resident.id
    second_site = build.system.system_map.get_location("lh/carrier-25-1")
    second_site.initialize_labware(clone)
    build.system.add_labware(clone)

    lh_location = build.system.system_map.get_location("lh")
    with pytest.raises(DeckLabwareIdentityError):
        await build.system.reconcile_lh_deck_occupancy(lh_location)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_edit_location_moves_a_plate_between_two_sites_of_one_deck() -> None:
    """The operator's own move verb, within a single liquid handler.

    Claiming the target and clearing the source afterwards leaves the plate at
    both sites in between, and the deck reconcile refuses one name at two sites
    outright. So a projection driven from that gap does not merely arrive
    early: it raises, the operator's call fails, and the plate is left recorded
    at both ends.
    """
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, lh = await _build(recorder, wf_name="recon_edit_within_deck")
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    try:
        snapshot = await runtime.labware.register(
            "reservoir", location=RESERVOIR_SITE, confirm=True,
        )

        await runtime.labware.edit_location(
            snapshot.id, "lh/carrier-7-0", reason="moved it by hand", confirm=True,
        )

        instance = next(lw for lw in build.system.labwares if lw.id == snapshot.id)
        holders = sorted(
            location.position_id for location in build.system.system_map.locations
            if location.labware is instance
        )
        assert holders == ["lh/carrier-7-0"], (
            "a plate recorded at two sites blocks reservations at both and "
            "makes every later reconcile of this device raise"
        )
        deck = await lh.driver.get_deck_state(GetDeckStateRequest())
        sites = {item.name: item.site for item in deck.labware}
        assert sites.get(instance.name) == "carrier-7-0", (
            f"the driver deck never learned the new site; it reports {sites!r}"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_edit_location_onto_a_busy_site_leaves_the_source_holding_it() -> None:
    """A refused target costs the operator nothing but the error.

    The target slot is the only step of a relocation that can refuse, so it is
    claimed before the source is released and a busy one leaves the whole
    record where it was. That is what makes re-issuing the command safe, and
    nothing else pins it: the ordering around it has been rewritten more than
    once, and each rewrite can trade this away without a test going red.
    """
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await _build(recorder, wf_name="recon_edit_busy_target")
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    try:
        occupant = await runtime.labware.register(
            "reservoir", location="lh/carrier-7-0", confirm=True,
        )
        mover = await runtime.labware.register(
            "reservoir", location=RESERVOIR_SITE, confirm=True,
        )

        with pytest.raises(SlotOccupiedError):
            await runtime.labware.edit_location(
                mover.id, "lh/carrier-7-0", reason="moved it by hand", confirm=True,
            )

        moved = next(lw for lw in build.system.labwares if lw.id == mover.id)
        sitting = next(lw for lw in build.system.labwares if lw.id == occupant.id)
        holders = sorted(
            location.position_id for location in build.system.system_map.locations
            if location.labware is moved
        )
        assert holders == [RESERVOIR_SITE], (
            "a refused move must leave the source holding it, or the operator "
            "re-issues the command against a record that already lost the plate"
        )
        assert build.system.labware_location_service.get(moved).position_id == (
            RESERVOIR_SITE
        )
        assert build.system.system_map.get_location(
            "lh/carrier-7-0"
        ).labware is sitting, "the refusing site kept its own occupant"
    finally:
        await runtime.shutdown()
