"""An operator write about one labware touches only that labware on the deck.

On the bench, correcting the tip count on one rack re-projected the whole
liquid handler: the driver was told to wipe its deck and rebuild it, so every
other plate and rack on that deck was destroyed and re-created to fix a number
on one of them. On a live instrument that is a burst of real wire commands
about labware nobody asked to touch, and a rebuild that fails partway leaves
the deck half-projected with no rollback.

The deck-wide reconcile is the right tool for a driver world whose occupancy is
unknown (freshly configured, or an earlier reconcile never landed). It is the
wrong tool for a delta the engine can name, and every operator labware verb
names one.
"""

import pytest
from pydantic import JsonValue

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import (
    DeckLayoutConfig,
    DeckResourceConfig,
    RecordingLiquidHandlerDriver,
    Teachpoint,
)
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from cheshire_drivers.sims import RecordedCall
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate

DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
    ],
)

PLATE_SITE = "lh/carrier-7-0"
SPARE_PLATE_SITE = "lh/carrier-7-1"
RACK_SITE = "lh/carrier-15-0"


class _LhFactory:
    """Hand every liquid handler the recording driver; sim defaults elsewhere."""

    def __init__(self, lh: RecordingLiquidHandlerDriver) -> None:
        self._lh = lh
        from orca.runtime.device_factory import SimDeviceFactory
        self._fallback = SimDeviceFactory()

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        if device_type == "liquid_handler":
            return self._lh, self._lh
        return self._fallback.build_drivers(device_type, name, deck_modeling=deck_modeling)


async def _deck_with_a_rack_and_a_plate(recorder: RecordingLiquidHandlerDriver):
    """A liquid handler holding one tip rack and one plate, both registered by
    an operator. Returns the handler, the runtime, and each labware snapshot."""
    stores = InMemoryRuntimeStoreFactory()
    plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    tips = TipRackTemplate(
        "tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)

    with use_device_factory(_LhFactory(recorder)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG}),
            deck_layout="default",
        )
        pad = Storage("pad")
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("pad", C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("lh/carrier-7-2", C(400, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    build = await orca.build_system(
        name="TargetedWrite",
        topology=Topology(locations={"lh": lh, "pad": pad}, transporters=[arm]),
        stores=stores,
        labwares=[plate, tips],
    )
    runtime = SystemRuntime(
        build.system, event_bus=build.event_bus, labware_store=InMemoryLabwareStore(),
    )
    await runtime.start()
    rack = await runtime.labware.register("tips", location=RACK_SITE, confirm=True)
    held_plate = await runtime.labware.register(
        "sample_plate", location=PLATE_SITE, confirm=True)
    return lh, runtime, rack, held_plate


def _labware_the_driver_was_told_about(calls: list[RecordedCall]) -> set[str]:
    """Every labware name the recorded deck calls name.

    A deck-wide reconcile names every occupant it rebuilds, so a write that
    only concerns one rack shows up here as the whole deck.
    """
    names: set[str] = set()
    for call in calls:
        if call.method == "reconcile_deck_occupancy":
            resources = call.args["resources"]
            assert isinstance(resources, list)
            for resource in resources:
                assert isinstance(resource, dict)
                names.add(str(resource["name"]))
        elif call.method in ("add_deck_labware", "remove_deck_labware"):
            names.add(str(call.args["name"]))
    return names


def _deck_entries_for(calls: list[RecordedCall], labware_name: str) -> list[JsonValue]:
    """Every recorded introduction of this labware, whichever verb carried it."""
    entries: list[JsonValue] = []
    for call in calls:
        if call.method == "add_deck_labware" and call.args["name"] == labware_name:
            entries.append(call.args)
        elif call.method == "reconcile_deck_occupancy":
            resources = call.args["resources"]
            assert isinstance(resources, list)
            entries.extend(
                res for res in resources
                if isinstance(res, dict) and res.get("name") == labware_name
            )
    return entries


def _tip_layout_pushed_for(
    calls: list[RecordedCall], labware_name: str,
) -> dict[str, bool] | None:
    """The tip layout the last recorded introduction carried for this rack."""
    layout: dict[str, bool] | None = None
    for entry in _deck_entries_for(calls, labware_name):
        assert isinstance(entry, dict)
        well_state = entry.get("well_state")
        if not isinstance(well_state, dict):
            continue
        tips = well_state.get("tips")
        if isinstance(tips, dict):
            layout = {str(pos): bool(present) for pos, present in tips.items()}
    return layout


@pytest.mark.timeout(30)
async def test_a_tip_state_write_does_not_reproject_the_plate_beside_it() -> None:
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    _, runtime, rack, plate = await _deck_with_a_rack_and_a_plate(recorder)
    try:
        recorder.calls.clear()
        await runtime.labware.set_tip_state(
            rack.id, ["A1", "B1"], reason="counted the rack by hand", confirm=True,
        )

        touched = _labware_the_driver_was_told_about(recorder.calls)
        assert rack.name in touched, (
            f"the corrected rack must reach the driver; driver saw "
            f"{[c.method for c in recorder.calls]}"
        )
        assert plate.name not in touched, (
            f"correcting one rack re-projected {plate.name}, which the operator "
            f"never mentioned; driver saw {[c.method for c in recorder.calls]}"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_a_tip_state_write_still_carries_the_layout_to_the_driver() -> None:
    """Narrowing the push must not quietly drop it: the rack still reaches the
    driver carrying the tip layout the operator asserted."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    _, runtime, rack, _ = await _deck_with_a_rack_and_a_plate(recorder)
    try:
        recorder.calls.clear()
        await runtime.labware.set_tip_state(
            rack.id, ["A1", "B1"], reason="counted the rack by hand", confirm=True,
        )

        seeded = _tip_layout_pushed_for(recorder.calls, rack.name)
        assert seeded is not None, (
            f"no tip layout reached the driver for {rack.name}; driver saw "
            f"{[c.method for c in recorder.calls]}"
        )
        assert sorted(pos for pos, present in seeded.items() if present) == ["A1", "B1"]
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_discharging_one_labware_leaves_its_neighbour_projected() -> None:
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    _, runtime, rack, plate = await _deck_with_a_rack_and_a_plate(recorder)
    try:
        recorder.calls.clear()
        await runtime.labware.discharge_labware(plate.id, force=True)

        touched = _labware_the_driver_was_told_about(recorder.calls)
        assert rack.name not in touched, (
            f"discharging {plate.name} re-projected {rack.name}; driver saw "
            f"{[c.method for c in recorder.calls]}"
        )
        assert plate.name in touched, "the discharged plate must leave the driver deck"
        assert "configure_deck" not in [c.method for c in recorder.calls], (
            f"taking {plate.name} off a deck that already has its layout re-sent "
            f"the layout; driver saw {[c.method for c in recorder.calls]}"
        )
        state = await recorder.get_deck_state(GetDeckStateRequest())
        on_deck = {item.name for item in state.labware}
        assert rack.name in on_deck and plate.name not in on_deck
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_moving_a_labware_across_the_deck_leaves_it_at_the_new_site() -> None:
    """edit_location places at the target and then clears the source. The
    source clear must not un-materialize the labware the placement just put
    down one site over."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    _, runtime, rack, plate = await _deck_with_a_rack_and_a_plate(recorder)
    try:
        recorder.calls.clear()
        await runtime.labware.edit_location(
            plate.id, SPARE_PLATE_SITE, reason="moved it by hand", confirm=True,
        )

        touched = _labware_the_driver_was_told_about(recorder.calls)
        assert rack.name not in touched, (
            f"moving {plate.name} re-projected {rack.name}; driver saw "
            f"{[c.method for c in recorder.calls]}"
        )
        state = await recorder.get_deck_state(GetDeckStateRequest())
        assert plate.name in {item.name for item in state.labware}, (
            "the moved plate must still be on the driver deck after the source clear"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_a_write_into_a_rebuilt_driver_session_rebuilds_the_whole_deck() -> None:
    """A driver session that came back holds an empty deck, so there is nothing
    for a single-labware delta to land on. The first write after that has to
    re-project everything, or the labware it did not mention stays invisible to
    the instrument."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    lh, runtime, rack, plate = await _deck_with_a_rack_and_a_plate(recorder)
    try:
        await lh.invalidate_deck_world()
        recorder.calls.clear()
        await runtime.labware.edit_location(
            plate.id, SPARE_PLATE_SITE, reason="moved it by hand", confirm=True,
        )

        assert rack.name in _labware_the_driver_was_told_about(recorder.calls), (
            f"the rack the write never mentioned must be re-projected into the "
            f"rebuilt session; driver saw {[c.method for c in recorder.calls]}"
        )
        state = await recorder.get_deck_state(GetDeckStateRequest())
        on_deck = {item.name for item in state.labware}
        assert {rack.name, plate.name} <= on_deck, f"deck held only {on_deck}"
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_clear_all_still_wipes_the_whole_deck() -> None:
    """The panic button stays deck-wide: narrowing single-labware writes must
    not narrow the one verb that is supposed to reach everything."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    _, runtime, rack, plate = await _deck_with_a_rack_and_a_plate(recorder)
    try:
        await runtime.labware.clear_all_labware(force=True)

        state = await recorder.get_deck_state(GetDeckStateRequest())
        on_deck = {item.name for item in state.labware}
        assert not on_deck & {rack.name, plate.name}, (
            f"clear-all left {on_deck & {rack.name, plate.name}} on the deck"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_taking_a_labware_off_a_bare_driver_world_rebuilds_the_deck() -> None:
    """A removal is still a deck write, so it meets a rebuilt driver session
    the same way an arrival does: lay the deck out, then re-project the ledger,
    which by then already excludes the labware that left."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    lh, runtime, rack, plate = await _deck_with_a_rack_and_a_plate(recorder)
    try:
        await lh.invalidate_deck_world()
        recorder.calls.clear()
        await runtime.labware.discharge_labware(plate.id, force=True)

        pushed = [call.method for call in recorder.calls]
        assert "configure_deck" in pushed, (
            f"the rebuilt session was never given its deck layout back; driver "
            f"saw {pushed}"
        )
        state = await recorder.get_deck_state(GetDeckStateRequest())
        on_deck = {item.name for item in state.labware}
        assert rack.name in on_deck, (
            f"the rack the removal never mentioned must be re-projected into the "
            f"rebuilt session; deck held {on_deck}"
        )
        assert plate.name not in on_deck, (
            f"the discharged plate came back onto the deck; deck held {on_deck}"
        )
    finally:
        await runtime.shutdown()
