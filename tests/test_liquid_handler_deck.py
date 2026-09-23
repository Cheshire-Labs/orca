"""Integration tests for LiquidHandler -- full deck lifecycle.

Tests the complete flow:
1. Arm delivers the plate to one of the deck sites it is taught
2. It materializes there, then the internal gripper moves it to the specified
   deck slot (via _do_notify_placed: add_deck_labware + move_plate)
3. Location service tracks the plate at that deck site node
4. Action body executes (pipetting)
5. Plate departs (prepare_for_pick relays it back to an arm-taught site,
   _do_notify_picked un-materializes it from the deck projection)
6. Location service tracks the plate back off the device

No site is a designated handoff: the arm reaches exactly the sites it teaches
and the gripper meshes the deck, so entry/exit sites are derived, not declared.
"""

import pytest

import orca.orca as orca
from orca.spawn import DISPENSE
from cheshire_drivers import (
    CartesianCoordinates,
    DeckLayoutConfig, DeckResourceConfig,
    Teachpoint,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.resource_models.labware import LabwareInitialState
from orca.workflow_models.status_enums import FailurePolicy
from tests.test_helpers import run_to_quiescence, named_for_template

# Carriers-only deck (structure). Occupancy is NOT declared here; transient
# labware materializes via @orca.action(deck_positions=...) and leaves on depart.
DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
    ],
)

# No site is a designated handoff, so a transit enters at whichever taught site
# the router picks. Must match the "lh/..." teachpoints seeded on `arm` below.
ARM_TAUGHT_SITES = ("carrier-7-2", "carrier-15-1")


async def build_deck_test_system(
    recorder: RecordingLiquidHandlerDriver,
    *,
    sample_initial_state: LabwareInitialState | None = None,
) -> SystemBuild:
    """Minimal system with a LiquidHandler to test deck lifecycle."""

    stores = InMemoryRuntimeStoreFactory()

    sample_plate = PlateTemplate(
        "sample_plate",
        labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=sample_initial_state,
    )
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)

    class _LhDeckFactory:
        """Inject the recording LH; sim defaults for everything else."""

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

    with use_device_factory(_LhDeckFactory(recorder)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG}),
            deck_layout="default",
        )
        stacker = Storage("stacker")
        tip_stacker = Storage("tip_stacker")
        waste = Storage("waste")
        pad = PlatePad("pad")

        c = CartesianCoordinates
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker", c(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("tip_stacker", c(0, 400, 300, 0, 90, 180), orientation="right"),
                Teachpoint("pad", c(200, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("lh/carrier-7-2", c(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("lh/carrier-15-1", c(400, 400, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", c(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(
        device=lh,
        inputs=[sample_plate, tips],
        deck_positions={sample_plate: "carrier-7-0", tips: "carrier-15-0"},
        failure_policy=FailurePolicy.ABORT,
    )
    async def pipette_step(ctx) -> None:
        handler = ctx.device(ILiquidHandler)
        plate = ctx.labware("sample_plate").plate
        rack = ctx.labware("tips").tip_rack
        await handler.pick_up_tips([rack.tip_spot("A1")])
        await handler.aspirate([plate.well("A1")], [10.0])
        await handler.dispense([plate.well("B1")], [10.0])
        await handler.drop_tips([rack.tip_spot("A1")])

    @orca.method
    async def pipette_method(ctx) -> None:
        yield pipette_step

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx) -> None:
        yield pipette_method

    # A tip rack is TRANSIENT: it routes in through the lab, materializes at
    # its tip-carrier deck site (not the plate handoff), is used, and leaves.
    @orca.thread(labware=tips, start=("tip_stacker", DISPENSE), end="waste")
    async def tips_journey(ctx) -> None:
        yield orca.join(allows=[pipette_method])

    @orca.workflow(name="deck_test")
    def deck_test_wf(wf) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    topology = Topology(
        locations={
            "stacker": stacker,
            "tip_stacker": tip_stacker,
            "pad": pad,
            "lh": lh,
            "waste": waste,
        },
        transporters=[arm],
    )
    return await orca.build_system(
        name="Deck Test",
        workflow=deck_test_wf,
        topology=topology,
        stores=stores,
    )


async def _run_deck_workflow(
    recorder: RecordingLiquidHandlerDriver,
) -> tuple[SystemRuntime, LiquidHandler, dict[str, str]]:
    """Build, start, submit, and drive the deck workflow to quiescence.

    Uses SystemRuntime (not SystemBuild.run) because the resident tip rack
    binds via start_reuse_existing, which needs the runtime's labware store.
    """
    build = await build_deck_test_system(recorder)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow("deck_test", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, record.id)
    lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
    return runtime, lh, statuses


class TestLiquidHandlerDeckLifecycle:

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_deck_lifecycle_runs_in_sim(self) -> None:
        """Full deck lifecycle: arrive -> gripper to slot -> pipette -> depart."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        _, _, statuses = await _run_deck_workflow(recorder)

        assert statuses, "no threads were created"
        assert all(s == "COMPLETED" for s in statuses.values()), statuses

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_deck_move_plate_called_on_arrival(self) -> None:
        """move_plate is called when plate arrives at LH with deck_positions."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        await _run_deck_workflow(recorder)

        move_calls = [c for c in recorder.calls if c.method == "move_plate"]
        assert len(move_calls) >= 1, f"Expected move_plate calls, got: {[c.method for c in recorder.calls]}"
        # Keyed to the sample plate: the tip rack transits concurrently, so
        # indexing the shared call list would assert on whichever thread won.
        plate_moves = [c for c in move_calls if named_for_template(c.args["plate"], "sample_plate")]
        assert any(c.args["to_position"] == "carrier-7-0" for c in plate_moves), (
            f"the gripper must relay the sample plate to its declared deck position "
            f"carrier-7-0; sample-plate move_plate calls were {plate_moves}")

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_deck_move_plate_called_on_departure(self) -> None:
        """move_plate is called when the plate departs the LH: the gripper relays
        it off its working site to a site the arm can actually reach."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        await _run_deck_workflow(recorder)

        move_calls = [c for c in recorder.calls if c.method == "move_plate"]
        assert len(move_calls) >= 2, f"Expected 2+ move_plate calls, got {len(move_calls)}"
        # Keyed to the sample plate: a bare membership check over every plate's
        # destinations passes on the tip rack's transit alone.
        plate_moves = [c for c in move_calls if named_for_template(c.args["plate"], "sample_plate")]
        assert any(
            c.args["from_position"] == "carrier-7-0"
            and c.args["to_position"] in ARM_TAUGHT_SITES
            for c in plate_moves
        ), (
            f"the sample plate must be relayed off carrier-7-0 to an arm-taught site "
            f"{ARM_TAUGHT_SITES} to depart; sample-plate move_plate calls were "
            f"{plate_moves}")

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_pipetting_commands_reach_driver(self) -> None:
        """Verify pipetting commands execute between gripper moves."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        await _run_deck_workflow(recorder)

        methods = [c.method for c in recorder.calls]
        assert "pick_up_tips" in methods
        assert "aspirate" in methods
        assert "dispense" in methods
        assert "drop_tips" in methods

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_transient_plate_materialized_then_unmaterialized(self) -> None:
        """The driver-deck projection materializes the transient plate on arrival
        (add_deck_labware) and un-materializes it on departure (remove_deck_labware);
        after the run the plate is gone from the deck while the carriers survive."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        _, lh, statuses = await _run_deck_workflow(recorder)
        assert all(s == "COMPLETED" for s in statuses.values()), statuses

        placed = [c for c in recorder.calls if c.method == "add_deck_labware"]
        unloaded = [c for c in recorder.calls if c.method == "remove_deck_labware"]
        assert any(named_for_template(c.args["name"], "sample_plate") for c in placed), (
            f"sample_plate was never materialized: add_deck_labware calls={placed}"
        )
        assert any(named_for_template(c.args["name"], "sample_plate") for c in unloaded), (
            f"sample_plate was never un-materialized: remove_deck_labware calls={unloaded}"
        )

        deck = await lh.driver.get_deck_state(GetDeckStateRequest())
        names = {item.name for item in deck.labware}
        assert not any(named_for_template(n, "sample_plate") for n in names), (
            f"transient plate still on the deck after departure: {names}"
        )
        assert "carrier-7" in names and "carrier-15" in names, (
            f"carriers must survive as structure: {names}"
        )

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_transient_tip_rack_transits_an_arm_taught_site(self) -> None:
        """A TRANSIENT tip rack rides the same generic flow as a plate: the arm
        delivers it to one of the sites it teaches, the internal gripper relays it
        to the declared target tip site (carrier-15-0), then back out to an
        arm-reachable site, and it is un-materialized on departure. Entry is not
        pinned to the rack's own carrier: no site is a designated handoff, so the
        router picks among the taught sites."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        _, lh, statuses = await _run_deck_workflow(recorder)
        assert all(s == "COMPLETED" for s in statuses.values()), statuses

        placed = [c for c in recorder.calls if c.method == "add_deck_labware"]
        unloaded = [c for c in recorder.calls if c.method == "remove_deck_labware"]
        moved = [c for c in recorder.calls if c.method == "move_plate"]

        tip_places = [c for c in placed if named_for_template(c.args["name"], "tips")]
        assert tip_places, f"tip rack was never materialized: add_deck_labware calls={placed}"
        # Materialized where the ARM put it, never at an interior site it cannot reach.
        entries = {c.args["at"] for c in tip_places}
        assert entries <= set(ARM_TAUGHT_SITES), (
            f"tip rack materialized at a site the arm cannot reach: {entries}; "
            f"taught sites are {ARM_TAUGHT_SITES}"
        )
        # The rack DOES ride the gripper, entry site -> target tip site and back out.
        tip_moves = [c for c in moved if named_for_template(c.args["plate"], "tips")]
        assert any(
            c.args["from_position"] in ARM_TAUGHT_SITES
            and c.args["to_position"] == "carrier-15-0"
            for c in tip_moves
        ), f"tip rack was not shuttled from its entry site to its target site: {tip_moves}"
        assert any(c.args["to_position"] in ARM_TAUGHT_SITES for c in tip_moves), (
            f"tip rack was not relayed back to an arm-reachable site to depart: {tip_moves}"
        )
        assert any(named_for_template(c.args["name"], "tips") for c in unloaded), (
            f"tip rack was never un-materialized: remove_deck_labware calls={unloaded}"
        )

        deck = await lh.driver.get_deck_state(GetDeckStateRequest())
        names = {item.name for item in deck.labware}
        assert not any(named_for_template(n, "tips") for n in names), (
            f"transient tip rack still on the deck after departure: {names}"
        )
        assert "carrier-7" in names and "carrier-15" in names, (
            f"carriers must survive as structure: {names}"
        )

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_departure_closes_gripper_after_unmaterialize(self) -> None:
        """_do_notify_picked must still close the gripper on the deck-resident
        departure path (the base contract), not only when nothing was on deck:
        every remove_deck_labware is immediately followed by a close so the
        gripper is never left open on hardware after a pick."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        _, _, statuses = await _run_deck_workflow(recorder)
        assert all(s == "COMPLETED" for s in statuses.values()), statuses

        methods = [c.method for c in recorder.calls]
        removes = [i for i, m in enumerate(methods) if m == "remove_deck_labware"]
        assert removes, f"no remove_deck_labware recorded: {methods}"
        for i in removes:
            assert methods[i + 1 : i + 2] == ["close"], (
                f"remove_deck_labware at index {i} not immediately followed by close: "
                f"{methods[i : i + 2]}"
            )

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_initial_state_rides_add_deck_labware_wire(self) -> None:
        """A plate authored with initial_state must carry its declared well
        volumes on the add_deck_labware introduction wire, so the driver seeds
        the tracker at materialization. RED until the bridge resolves
        template.initial_state onto the occupancy wire.

        Only the author-specified wells ride the wire: padding the unspecified
        wells with zeros would strict-track every well and break the
        lenient-by-default behavior the rest of the deck relies on.
        """
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_deck_test_system(
            recorder, sample_initial_state=LabwareInitialState(wells={"A1": 123.0}),
        )
        runtime = SystemRuntime(build.system, event_bus=build.event_bus)
        await runtime.start()
        try:
            record = await runtime.submit_workflow("deck_test", mode=WorkflowRunMode.PURE_SIM)
            statuses = await run_to_quiescence(runtime, record.id)
            assert all(s == "COMPLETED" for s in statuses.values()), statuses

            add_calls = [
                c for c in recorder.calls
                if c.method == "add_deck_labware" and named_for_template(c.args["name"], "sample_plate")
            ]
            assert add_calls, "sample_plate was never materialized"
            well_state = add_calls[0].args.get("well_state")
            assert well_state is not None, (
                "add_deck_labware did not carry initial well_state from "
                "template.initial_state (the dead bridge)"
            )
            assert well_state["volumes"] == {"A1": 123.0}, (
                "bridge must carry only the author-specified wells, not the "
                f"zero-padded resolver map: {well_state['volumes']}"
            )
        finally:
            await runtime.shutdown()


async def build_selected_deck_system() -> SystemBuild:
    """Like ``build_deck_test_system`` but with NO injected driver: the
    deck-modeling sim slot is chosen by the factory from the device class.

    This exercises the PR3a selection path itself. The deck-modeling
    ``LiquidHandler`` must resolve to a deck-modeling sim (Chatterbox composite,
    ``provides_state=True``) under PURE_SIM rather than the no-op protocol sim,
    so the in-process e2e is no longer a false positive.
    """
    stores = InMemoryRuntimeStoreFactory()

    sample_plate = PlateTemplate("sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)

    lh = LiquidHandler(
        "lh",
        deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG}),
        deck_layout="default",
    )
    stacker = Storage("stacker")
    tip_stacker = Storage("tip_stacker")
    waste = Storage("waste")
    pad = PlatePad("pad")

    c = CartesianCoordinates
    arm = Transporter(
        "arm",
        teachpoint_store=stores.teachpoints("arm", seed=[
            Teachpoint("stacker", c(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("tip_stacker", c(0, 400, 300, 0, 90, 180), orientation="right"),
            Teachpoint("pad", c(200, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("lh/carrier-7-2", c(400, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("lh/carrier-15-1", c(400, 400, 300, 0, 90, 180), orientation="right"),
            Teachpoint("waste", c(600, 200, 300, 0, 90, 180), orientation="right"),
        ]),
    )

    @orca.action(
        device=lh,
        inputs=[sample_plate, tips],
        deck_positions={sample_plate: "carrier-7-0", tips: "carrier-15-0"},
        failure_policy=FailurePolicy.ABORT,
    )
    async def pipette_step(ctx) -> None:
        handler = ctx.device(ILiquidHandler)
        plate = ctx.labware("sample_plate").plate
        rack = ctx.labware("tips").tip_rack
        await handler.pick_up_tips([rack.tip_spot("A1")])
        await handler.aspirate([plate.well("A1")], [10.0])
        await handler.dispense([plate.well("B1")], [10.0])
        await handler.drop_tips([rack.tip_spot("A1")])

    @orca.method
    async def pipette_method(ctx) -> None:
        yield pipette_step

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx) -> None:
        yield pipette_method

    @orca.thread(labware=tips, start=("tip_stacker", DISPENSE), end="waste")
    async def tips_journey(ctx) -> None:
        yield orca.join(allows=[pipette_method])

    @orca.workflow(name="deck_test")
    def deck_test_wf(wf) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    topology = Topology(
        locations={"stacker": stacker, "tip_stacker": tip_stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="Selected Deck Test",
        workflow=deck_test_wf,
        topology=topology,
        stores=stores,
    )


class TestPureSimDeckModelingSelection:
    """PR3a: a deck-modeling LiquidHandler under PURE_SIM is no longer a no-op."""

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_deck_modeling_lh_selected_under_pure_sim(self) -> None:
        """The factory selects a deck-modeling sim (provides_state True) for a
        LiquidHandler, and after configure_deck self-initializes during lazy
        init the driver reports a REAL deck state (carriers present, transient
        plate materialized and reconciled), proving PURE_SIM exercises a deck."""
        build = await build_selected_deck_system()
        runtime = SystemRuntime(build.system, event_bus=build.event_bus)
        await runtime.start()
        try:
            lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))

            # Selection proof: the sim slot is the deck-modeling composite, not
            # the no-op SimLiquidHandlerWithProtocolDriver.
            assert lh.driver.provides_state is True, (
                f"PURE_SIM LH wired a no-op sim ({type(lh.driver).__name__}); "
                f"expected a deck-modeling sim with provides_state=True"
            )
            assert type(lh.driver).__name__ == "ChatterboxLiquidHandlerWithProtocolDriver", (
                f"unexpected sim driver: {type(lh.driver).__name__}"
            )

            record = await runtime.submit_workflow("deck_test", mode=WorkflowRunMode.PURE_SIM)
            statuses = await run_to_quiescence(runtime, record.id)
            assert all(s == "COMPLETED" for s in statuses.values()), statuses

            # configure_deck at lazy init built a real PLR deck, so carriers
            # are present; a no-op sim reports an empty deck regardless.
            deck = await lh.driver.get_deck_state(GetDeckStateRequest())
            names = {item.name for item in deck.labware}
            assert "carrier-7" in names and "carrier-15" in names, (
                f"deck-modeling sim reported no carriers (no-op behavior): {names}"
            )
        finally:
            await runtime.shutdown()
