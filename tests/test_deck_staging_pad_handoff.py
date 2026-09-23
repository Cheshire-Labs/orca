"""A Flex staging pad (column 4) as the arm's handoff site, used more than once.

The runtime learns what the driver world holds only through ``get_deck_state``,
and ``_do_notify_picked`` un-materializes a departing labware only when that
read reports it. So a pad the driver hides from the read is a pad the arm can
never empty in the driver world: the resident stays in the PLR tree after the
pick, and the deck gripper's next drop onto that pad is refused as occupied.
The bench sequence that surfaced it: rack 1 out through D4, rack 2 in through
B4 and back out to D4, refused with the first rack still parented there.
"""
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from cheshire_drivers import (
    CartesianCoordinates, DeckLayoutConfig, RecordingLiquidHandlerDriver, Teachpoint,
)
from cheshire_drivers.liquid_handler_models import MovePlateRequest
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.labware import TipRackTemplate
from orca.sdk.workflow import MethodTemplate
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.mock import EXTERNAL_MOVER
from tests.test_helpers import run_to_quiescence

FLEX_DECK = DeckLayoutConfig(deck_type="FlexDeck", resources=[])
STAGING_HANDOFF = "D4-slot"
WORKING_SLOT = "B2-slot"


class _FlexFactory:
    def __init__(self, driver: RecordingLiquidHandlerDriver) -> None:
        self._driver = driver
        self._fallback = SimDeviceFactory()

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        if device_type == "liquid_handler":
            return self._driver, self._driver
        return self._fallback.build_drivers(device_type, name, deck_modeling=deck_modeling)


async def _build(recorder: RecordingLiquidHandlerDriver) -> SystemBuild:
    """One tip-rack template, a Flex whose only arm-taught site is a staging pad."""
    stores = InMemoryRuntimeStoreFactory()
    rack = TipRackTemplate("rack", labware_type="flex_96_tiprack_200ul", with_tips=True)

    with use_device_factory(_FlexFactory(recorder)):
        flex = LiquidHandler(
            "flex",
            deck_layout_store=stores.deck_layouts("flex", seed={"default": FLEX_DECK}),
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
                Teachpoint(f"flex/{STAGING_HANDOFF}", c(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", c(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(
        device=flex,
        inputs=[rack],
        deck_positions={rack: WORKING_SLOT},
        failure_policy=FailurePolicy.ABORT,
    )
    async def deck_step(ctx: ActionContext) -> None:
        ctx.device(ILiquidHandler)

    @orca.method
    async def deck_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield deck_step

    @orca.thread(labware=rack, start=("stacker", DISPENSE), end="waste")
    async def rack_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield deck_method

    @orca.workflow(name="staging_roundtrip")
    def wf(w: WorkflowContext) -> None:
        w.start(rack_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "flex": flex, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="Staging Pad Handoff", workflow=wf, topology=topology, stores=stores,
    )


class TestArmPickFromAStagingPadEmptiesItInTheDriverWorld:
    """Hook level: the departure hook must un-materialize a staging resident, and
    the pad must then accept a gripper drop. Driven directly so the assertion is
    reachable even when the workflow path would only surface it as a crash."""

    @pytest.mark.asyncio
    async def test_departure_sends_remove_and_the_pad_accepts_the_next_drop(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await _build(recorder)
        flex = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        await flex.deck_world_layout()
        template = build.system.get_labware_template("rack")
        first = await template.create_instance()
        await first.enter_record(build.system.labware_contents)
        second = await template.create_instance()
        await second.enter_record(build.system.labware_contents)
        arm = EXTERNAL_MOVER

        await flex._do_notify_placed(first, arm, target=STAGING_HANDOFF)
        recorder.calls.clear()
        await flex._do_notify_picked(first, arm, target=STAGING_HANDOFF)

        assert [c.method for c in recorder.calls if c.method == "remove_deck_labware"], (
            "the arm's pick from the staging pad must un-materialize the rack; without it "
            "the driver world keeps the rack parented on the pad")

        await flex._do_notify_placed(second, arm, target=WORKING_SLOT)
        # The deck gripper's relay onto the pad the arm just emptied.
        await flex.driver.move_plate(MovePlateRequest(
            plate=second.name, from_position=WORKING_SLOT, to_position=STAGING_HANDOFF,
        ))


class TestTwoRacksThroughOneStagingPad:
    """Workflow level: two racks, each entering and leaving the Flex through the
    same staging pad, must both complete."""

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_the_second_rack_can_use_the_pad_the_first_left_through(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await _build(recorder)
        runtime = SystemRuntime(build.system, event_bus=build.event_bus)
        await runtime.start()
        try:
            for _ in range(2):
                record = await runtime.submit_workflow(
                    "staging_roundtrip", mode=WorkflowRunMode.PURE_SIM,
                )
                statuses = await run_to_quiescence(runtime, record.id)
                assert all(s == "COMPLETED" for s in statuses.values()), statuses
        finally:
            await runtime.shutdown()
