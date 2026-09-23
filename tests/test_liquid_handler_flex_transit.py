"""Transit-delivered labware onto an Opentrons Flex deck via a derived handoff.

There is no declared handoff. Deck sites are equal flat nodes and the external
arm reaches exactly the sites it is taught, by SITE-QUALIFIED teachpoint. A
Flex deck has one physical arm-accessible coordinate here, so the arm is taught
that one site; the internal gripper -- which meshes all deck sites pairwise --
relays each labware onward to the working slot named by the action's
``deck_positions``. Handoff-ness is therefore topology (which site the arm was
taught), not a declaration, and an arm-taught site is itself an ordinary
working site that can take direct delivery.

The OT-2 cases at the bottom pin both ends of that: taught the working slot, it
takes direct delivery with no relay; taught any other slot, the forced relay is
refused at run time because the deck has no gripper.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest

import orca.orca as orca
from orca.spawn import DISPENSE
from cheshire_drivers import (
    CartesianCoordinates,
    DeckLayoutConfig,
    Teachpoint,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.build import SystemBuild, Topology, build_system
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import run_to_quiescence, named_for_template

# A Flex deck declares no resources: its slots are built-in deck structure.
FLEX_DECK = DeckLayoutConfig(deck_type="FlexDeck", resources=[])

_ARM_SITE = "D3-slot"         # the one deck site the external arm is taught
_PLATE_SLOT = "C2-slot"       # the plate's working slot
_TIPS_SLOT = "B2-slot"        # the tip rack's working slot


class _LhDeckFactory:
    """Serves the recording driver for the liquid handler, sim drivers for the rest."""

    def __init__(self, lh: RecordingLiquidHandlerDriver) -> None:
        self._lh = lh
        self._fallback = SimDeviceFactory()

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        if device_type == "liquid_handler":
            return self._lh, self._lh
        return self._fallback.build_drivers(device_type, name, deck_modeling=deck_modeling)


async def build_flex_transit_system(recorder: RecordingLiquidHandlerDriver) -> SystemBuild:
    stores = InMemoryRuntimeStoreFactory()

    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
    )
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)

    with use_device_factory(_LhDeckFactory(recorder)):
        flex = LiquidHandler(
            "flex",
            deck_layout_store=stores.deck_layouts("flex", seed={"default": FLEX_DECK}),
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
                Teachpoint(f"flex/{_ARM_SITE}", c(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", c(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(
        device=flex,
        inputs=[sample_plate, tips],
        deck_positions={sample_plate: _PLATE_SLOT, tips: _TIPS_SLOT},
        failure_policy=FailurePolicy.ABORT,
    )
    async def pipette_step(ctx: ActionContext) -> None:
        handler = ctx.device(ILiquidHandler)
        plate = ctx.plate("sample_plate")
        rack = ctx.tip_rack("tips")
        await handler.pick_up_tips([rack.tip_spot("A1")])
        await handler.aspirate([plate.well("A1")], [10.0])
        await handler.dispense([plate.well("B1")], [10.0])
        await handler.drop_tips([rack.tip_spot("A1")])

    @orca.method
    async def pipette_method(ctx: MethodContext):
        yield pipette_step

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield pipette_method

    @orca.thread(labware=tips, start=("tip_stacker", DISPENSE), end="waste")
    async def tips_journey(ctx: ThreadContext):
        yield orca.join(allows=[pipette_method])

    @orca.workflow(name="flex_transit")
    def flex_wf(wf) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    topology = Topology(
        locations={
            "stacker": stacker, "tip_stacker": tip_stacker,
            "pad": pad, "flex": flex, "waste": waste,
        },
        transporters=[arm],
    )
    return await orca.build_system(
        name="Flex Transit Test", workflow=flex_wf, topology=topology, stores=stores,
    )


@asynccontextmanager
async def _run(
    recorder: RecordingLiquidHandlerDriver,
) -> AsyncIterator[tuple[LiquidHandler, dict[str, str]]]:
    """Run the Flex transit workflow to quiescence, shutting the runtime down after."""
    build = await build_flex_transit_system(recorder)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("flex_transit", mode=WorkflowRunMode.PURE_SIM)
        statuses = await run_to_quiescence(runtime, record.id)
        flex = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        yield flex, statuses
    finally:
        await runtime.shutdown()


class TestFlexArmTaughtSiteTransit:

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_transit_runs_to_completion(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        async with _run(recorder) as (_flex, statuses):
            assert statuses, "no threads were created"
            assert all(s == "COMPLETED" for s in statuses.values()), statuses

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_plate_materializes_at_the_arm_taught_site_then_shuttles_to_slot(self) -> None:
        """Every transit labware materializes at the site the arm was taught and
        the gripper relays it to its working slot -- the entry point is derived
        from the teachpoint, not declared per carrier."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        async with _run(recorder) as (_flex, statuses):
            assert all(s == "COMPLETED" for s in statuses.values()), statuses

            placed = [c for c in recorder.calls if c.method == "add_deck_labware"]
            moved = [c for c in recorder.calls if c.method == "move_plate"]

            # Both plate and tip rack drop at the SAME arm-taught site: it is the
            # only deck coordinate the arm can reach.
            for name in ("sample_plate", "tips"):
                drops = [c for c in placed if named_for_template(c.args["name"], name)]
                assert drops, f"{name} never materialized: {placed}"
                assert all(c.args["at"] == _ARM_SITE for c in drops), (
                    f"{name} materialized off the arm-taught site {_ARM_SITE!r}: {drops}"
                )

            # Gripper relays each from the arm-taught site to its own working slot.
            plate_in = [c for c in moved if named_for_template(c.args["plate"], "sample_plate")
                        and c.args["from_position"] == _ARM_SITE and c.args["to_position"] == _PLATE_SLOT]
            tips_in = [c for c in moved if named_for_template(c.args["plate"], "tips")
                       and c.args["from_position"] == _ARM_SITE and c.args["to_position"] == _TIPS_SLOT]
            assert plate_in, f"plate not relayed arm-site->slot: {moved}"
            assert tips_in, f"tips not relayed arm-site->slot: {moved}"

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_departure_shuttles_back_to_the_arm_taught_site_and_unmaterializes(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        async with _run(recorder) as (flex, statuses):
            assert all(s == "COMPLETED" for s in statuses.values()), statuses

            moved = [c for c in recorder.calls if c.method == "move_plate"]
            unloaded = [c for c in recorder.calls if c.method == "remove_deck_labware"]

            # On depart, each labware relays back to the arm-taught site for the arm.
            plate_out = [c for c in moved if named_for_template(c.args["plate"], "sample_plate")
                         and c.args["to_position"] == _ARM_SITE]
            assert plate_out, f"plate did not depart to the arm-taught site {_ARM_SITE!r}: {moved}"
            assert any(named_for_template(c.args["name"], "sample_plate") for c in unloaded), (
                f"sample_plate never un-materialized: {unloaded}"
            )

            deck = await flex.driver.get_deck_state(GetDeckStateRequest())
            names = {item.name for item in deck.labware}
            assert not any(named_for_template(n, t) for n in names for t in ("sample_plate", "tips")), (
                f"transit labware still on the deck after departure: {names}"
            )


class TestArmTeachpointWiring:
    """Which deck sites the arm can reach is stated by its teachpoints, and the
    build refuses any arm point that does not name exactly one real site."""

    async def _build(self, arm_teaches: str):
        stores = InMemoryRuntimeStoreFactory()
        flex = LiquidHandler(
            "flex",
            deck_layout_store=stores.deck_layouts("flex", seed={"default": FLEX_DECK}),
            deck_layout="default",
        )
        pad = PlatePad("pad")
        c = CartesianCoordinates
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint(arm_teaches, c(0, 0, 0, 0, 90, 180), orientation="right"),
                Teachpoint("pad", c(100, 0, 0, 0, 90, 180), orientation="right"),
            ]),
        )
        topology = Topology(locations={"flex": flex, "pad": pad}, transporters=[arm])
        return await build_system(name="t", topology=topology, stores=stores)

    async def test_arm_taught_site_is_an_ordinary_working_site(self) -> None:
        """The arm's entry point is not a reserved drop point: it is a plain deck
        site, gripper-meshed to its siblings, so it is a valid deck_positions
        target like any other slot."""
        build = await self._build(f"flex/{_ARM_SITE}")
        flex = build.topology.device("flex", LiquidHandler) if build.topology else None
        assert flex is not None

        sites = {site.position_id: site.resource for site in flex.sites}
        for name in (f"flex/{_ARM_SITE}", f"flex/{_PLATE_SLOT}", f"flex/{_TIPS_SLOT}"):
            assert isinstance(sites[name], DeviceDeckSite), f"{name} is not a deck site"

        system_map = build.system.system_map
        assert system_map.get_transporter_between(
            f"flex/{_ARM_SITE}", f"flex/{_PLATE_SLOT}"
        ).name == "flex/gripper"

    async def test_arm_teachpoint_must_name_a_real_slot(self) -> None:
        with pytest.raises(ValueError, match="neither a registered location"):
            await self._build("flex/Z9-slot")

    async def test_arm_teachpoint_must_be_site_qualified(self) -> None:
        """Teaching the bare device name is a build error, not a mid-run surprise.

        A deck handler has many physical arm coordinates, so the bare name
        cannot stand for the one the arm actually reaches. Catching it at build
        keeps it off the bench, where it would surface as a failed run with the
        plate already in the arm's grip.
        """
        with pytest.raises(ValueError, match="multi-site device"):
            await self._build("flex")


OT2_DECK = DeckLayoutConfig(deck_type="OTDeck", resources=[])
_OT2_ARM_SITE = "8-slot"      # a taught site that is NOT the working slot
_OT2_PLATE_SLOT = "7-slot"


async def build_ot2_system(
    recorder: RecordingLiquidHandlerDriver, arm_site: str,
) -> SystemBuild:
    """OT-2 topology whose arm is taught ``arm_site``. Teaching the working slot
    means direct delivery; teaching any other site forces an internal relay."""
    stores = InMemoryRuntimeStoreFactory()
    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
    )

    with use_device_factory(_LhDeckFactory(recorder)):
        ot2 = LiquidHandler(
            "ot2",
            deck_layout_store=stores.deck_layouts("ot2", seed={"default": OT2_DECK}),
            deck_layout="default",
        )
        stacker = Storage("stacker")
        waste = Storage("waste")
        pad = PlatePad("pad")
        c = CartesianCoordinates
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker", c(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("pad", c(200, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(f"ot2/{arm_site}", c(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", c(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(
        device=ot2,
        inputs=[sample_plate],
        deck_positions={sample_plate: _OT2_PLATE_SLOT},
        failure_policy=FailurePolicy.ABORT,
    )
    async def touch_step(ctx: ActionContext) -> None:
        ctx.device(ILiquidHandler)

    @orca.method
    async def touch_method(ctx: MethodContext):
        yield touch_step

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield touch_method

    @orca.workflow(name="ot2_transit")
    def ot2_wf(wf) -> None:
        wf.start(plate_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "ot2": ot2, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="OT-2 Transit", workflow=ot2_wf, topology=topology, stores=stores,
    )


async def _run_ot2(recorder: RecordingLiquidHandlerDriver, arm_site: str):
    build = await build_ot2_system(recorder, arm_site)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("ot2_transit", mode=WorkflowRunMode.PURE_SIM)
        return await asyncio.wait_for(runtime.wait(record.id), timeout=60.0)
    finally:
        await runtime.shutdown()


class TestOT2TransitRefused:
    """A gripperless OT-2 cannot fulfil an internal relay.

    Teaching the arm a site other than the working slot forces one, which is a
    configuration error on an OT-2, but build does NOT reject it: whether a
    handler has an internal gripper is a hardware fact, not derivable from
    deck_type (a Hamilton machine may or may not have one). So the
    misconfiguration surfaces at run time when the relay is attempted and
    refused. A topology-level gripper declaration that rejects it at build is
    still deferred.
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_ot2_relay_off_the_taught_site_is_refused_at_runtime(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        status = await _run_ot2(recorder, _OT2_ARM_SITE)

        assert status.status == ExecutionState.FAILED, (
            f"OT-2 transit must fail the execution, not complete or stall: {status.status}"
        )
        # Pin which half failed: the arm's drop succeeds, the gripper move
        # off the taught site is the step an OT-2 cannot do.
        add_at_arm_site = [
            c for c in recorder.calls
            if c.method == "add_deck_labware" and c.args["at"] == _OT2_ARM_SITE
        ]
        assert add_at_arm_site, (
            f"OT-2 transit never reached the arm-taught drop: {[c.method for c in recorder.calls]}"
        )
        relay = [
            c for c in recorder.calls
            if c.method == "move_plate" and c.args.get("to_position") == _OT2_PLATE_SLOT
        ]
        assert relay, (
            f"the gripper relay to {_OT2_PLATE_SLOT} was never attempted, so this test "
            f"is not exercising the refusal: {[c.method for c in recorder.calls]}"
        )


class TestOT2DirectDelivery:
    """An arm-taught site is itself a working site, so teaching the working slot
    needs no internal relay -- which is what keeps a gripperless deck usable.

    The model never forces a relay; it relays only when the action's
    ``deck_positions`` names a site the arm was not taught.
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_arm_delivers_straight_to_the_taught_working_slot(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        status = await _run_ot2(recorder, _OT2_PLATE_SLOT)

        assert status.status == ExecutionState.COMPLETED, (
            f"direct delivery to the taught working slot must complete: {status.status}"
        )
        add_at_slot = [
            c for c in recorder.calls
            if c.method == "add_deck_labware" and c.args["at"] == _OT2_PLATE_SLOT
        ]
        assert add_at_slot, (
            f"plate never materialized at the taught working slot: "
            f"{[c.method for c in recorder.calls]}"
        )
        assert not [c for c in recorder.calls if c.method == "move_plate"], (
            "no internal relay should be needed when the arm is taught the working slot itself"
        )
