"""The on-deck gripper hop, and what a device must NOT do during one.

A gripper hop moves a plate between two sites of one liquid handler. The plate
never leaves the device, so the door must not open and the driver world must not
be re-materialized -- ``move_plate`` already relocated it. An external arm doing
the same placement must get the full sequence.

The device tells the two apart by the ACTING MOVER: its own gripper means an
internal hop, anything else means someone reached in. That is derived from the
move, never tracked, because the gripper is built one per device with edges only
between that device's sites (``sdk/build.py`` ``add_gripper_edges``).

Single-labware fixture on purpose: ``open``/``close`` are recorded with no
arguments, so they cannot be attributed to a plate. The multi-labware fixture in
``test_liquid_handler_deck.py`` filters on ``args["plate"]`` for exactly that
reason, and that defence is unavailable here.
"""
import asyncio

import pytest

import orca.orca as orca
from orca.spawn import DISPENSE
from cheshire_drivers import (
    CartesianCoordinates, DeckLayoutConfig, DeckResourceConfig, Teachpoint,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.sims import RecordedCall
from cheshire_drivers.liquid_handler_models import (
    DeckStateResponse, GetDeckStateRequest, MovePlateRequest,
)
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import DeckLabwareIdentityError, LiquidHandler, Storage
from orca.resource_models.external_control import device_under_external_control
from orca.resource_models.labware import LabwareInstance, PlateInstance
from orca.runtime.sim_labware import SimPlate
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.resource_models.transporter_base import TransporterBase
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild, Topology
from orca.system.system_interface import DeckReconcileConflict
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.status_enums import FailurePolicy
from tests.mock import EXTERNAL_MOVER
from tests.test_helpers import run_to_quiescence

DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7)],
)

# The arm is taught one deck site; the gripper meshes the rest, so a plate
# always enters at carrier-7-2 and hops to its declared working slot.
ARM_TAUGHT_SITE = "carrier-7-2"
WORKING_SLOT = "carrier-7-0"

DOOR_AND_WORLD_CALLS = ("open", "close", "add_deck_labware", "remove_deck_labware")


class EmptyDeckStateDriver(RecordingLiquidHandlerDriver):
    """Reports an empty deck regardless of what was materialized, so
    ``_deck_holds`` is always False. Isolates the mover gate from the
    template-name idempotency fallback that otherwise masks it."""

    async def get_deck_state(self, request: GetDeckStateRequest) -> DeckStateResponse:
        await super().get_deck_state(request)
        return DeckStateResponse(labware=[])


async def build_single_plate_system(
    recorder: RecordingLiquidHandlerDriver,
) -> SystemBuild:
    """One plate, one deck carrier, one arm. No concurrent tip rack, so every
    recorded ``open``/``close`` is attributable to this plate's journey."""
    stores = InMemoryRuntimeStoreFactory()
    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
    )

    class _LhDeckFactory:
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
        waste = Storage("waste")
        pad = PlatePad("pad")
        c = CartesianCoordinates
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker", c(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("pad", c(200, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(f"lh/{ARM_TAUGHT_SITE}", c(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", c(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    # No pipetting: a tip rack to make aspirate legal would reintroduce the
    # concurrent second labware this fixture exists to avoid.
    @orca.action(
        device=lh,
        inputs=[sample_plate],
        deck_positions={sample_plate: WORKING_SLOT},
        failure_policy=FailurePolicy.ABORT,
    )
    async def deck_step(ctx) -> None:
        ctx.device(ILiquidHandler)

    @orca.method
    async def deck_method(ctx) -> None:
        yield deck_step

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx) -> None:
        yield deck_method

    @orca.workflow(name="hop_test")
    def hop_wf(wf) -> None:
        wf.start(plate_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="Gripper Hop Test", workflow=hop_wf, topology=topology, stores=stores,
    )



async def pick_and_record(build, gripper, source, labware) -> None:
    """What a move does: pick, then record the plate in the jaws through the
    placement chokepoint. The mover reads that record and never writes it."""
    await gripper.pick(source)
    await build.system.labware_placer.picked_up(labware, gripper.gripper_location)


async def run_single_plate(
    recorder: RecordingLiquidHandlerDriver,
) -> tuple[SystemRuntime, LiquidHandler, dict[str, str]]:
    build = await build_single_plate_system(recorder)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow("hop_test", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, record.id)
    lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
    return runtime, lh, statuses


def hop_count(recorder: RecordingLiquidHandlerDriver) -> int:
    """Gripper hops in this run, i.e. relays between two deck sites."""
    return len([c for c in recorder.calls if c.method == "move_plate"])


class TestInternalHopEmitsNoDeviceTraffic:
    """A hop by the device's own gripper must not open the door or touch the
    driver's world model."""

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_hop_emits_no_door_or_materialization_traffic(self) -> None:
        """Baseline the arm's own legitimate open/close, then assert the hops
        added none. Stated as a run-wide total derived from the fixture's
        actual hop count -- a hardcoded per-hop delta would not survive a
        fixture change."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        _, _, statuses = await run_single_plate(recorder)
        assert all(s == "COMPLETED" for s in statuses.values()), statuses

        hops = hop_count(recorder)
        assert hops >= 1, "fixture did not produce a gripper hop; the test proves nothing"

        opens = len([c for c in recorder.calls if c.method == "open"])
        # The arm enters once and departs once: two legitimate open/close pairs.
        # Each un-gated hook on a hop would add one more open on top.
        assert opens <= 2, (
            f"{hops} gripper hop(s) added door traffic: {opens} opens recorded, "
            f"expected at most the arm's own 2. Sequence: "
            f"{[c.method for c in recorder.calls]}"
        )

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_hop_does_not_rematerialize_when_deck_holds_reports_empty(self) -> None:
        """Re-materialization, isolated from the fallback.

        ``_do_notify_placed`` has two guards: the mover gate and the
        ``_deck_holds(labware.name)`` fallback. On today's unfixed code the
        fallback alone suppresses re-materialization, so a plain hop test stays
        green with the gate broken. Forcing ``_deck_holds`` False removes the
        fallback and leaves the gate as the only thing that can prevent a
        duplicate ``add_deck_labware``.
        """
        recorder = EmptyDeckStateDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        _, _, statuses = await run_single_plate(recorder)
        assert all(s == "COMPLETED" for s in statuses.values()), statuses

        hops = hop_count(recorder)
        assert hops >= 1, "fixture did not produce a gripper hop"
        adds = [c for c in recorder.calls if c.method == "add_deck_labware"]
        # One materialization at the arm-taught entry site; the hop must add none.
        assert len(adds) <= 1, (
            f"a gripper hop re-materialized the plate with _deck_holds forced False: "
            f"{len(adds)} add_deck_labware calls at {[c.args.get('at') for c in adds]}"
        )


class TestExternalArmStillGetsTheFullSequence:
    """Guards the gate against over-suppression: without this, a stuck-on gate
    passes every internal-hop assertion above."""

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_arm_arrival_and_departure_still_drive_the_device(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        _, _, statuses = await run_single_plate(recorder)
        assert all(s == "COMPLETED" for s in statuses.values()), statuses

        methods = [c.method for c in recorder.calls]
        assert "open" in methods, (
            "the external arm's own placement must still open the device; a gate "
            "that suppresses everything would make the hop tests pass trivially")
        assert "add_deck_labware" in methods, "the arm's delivery must materialize the plate"
        assert "remove_deck_labware" in methods, "the arm's departure must un-materialize it"


class TestDeckHoldsIdempotencyPreserved:
    """The ``_deck_holds`` fallback is the ONLY idempotency guard on the
    staging-bridge path, which has no mover gate at all. Pinning it here so a
    later cleanup does not delete it as redundant."""

    @pytest.mark.asyncio
    async def test_placement_of_already_present_template_does_not_duplicate(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        # Built, not hand-constructed: the builder binds the labware catalog,
        # without which create_instance refuses.
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        await lh.deck_world_layout()
        template = build.system.get_labware_template("sample_plate")
        labware = await template.create_instance()
        await labware.enter_record(build.system.labware_contents)
        mover = EXTERNAL_MOVER

        await lh._do_notify_placed(labware, mover, target=WORKING_SLOT)
        first = len([c for c in recorder.calls if c.method == "add_deck_labware"])
        await lh._do_notify_placed(labware, mover, target=WORKING_SLOT)
        second = len([c for c in recorder.calls if c.method == "add_deck_labware"])

        assert first == 1, "the first placement must materialize the plate"
        assert second == 1, (
            "a second placement of a template already in the driver deck state must "
            "not duplicate it; _deck_holds is the only guard on the bridge path")

    @pytest.mark.asyncio
    async def test_two_instances_of_one_template_hold_distinct_deck_resources(self) -> None:
        """Idempotency is per-INSTANCE, not per-template. The driver world keys
        deck labware by instance name, so two DISTINCT plates of one template
        materialize as two distinct driver resources at their own sites."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        await lh.deck_world_layout()
        template = build.system.get_labware_template("sample_plate")
        first_plate = await template.create_instance()
        await first_plate.enter_record(build.system.labware_contents)
        second_plate = await template.create_instance()
        await second_plate.enter_record(build.system.labware_contents)
        assert first_plate.id != second_plate.id
        assert first_plate.name != second_plate.name
        mover = EXTERNAL_MOVER

        await lh._do_notify_placed(first_plate, mover, target=WORKING_SLOT)
        await lh._do_notify_placed(second_plate, mover, target=ARM_TAUGHT_SITE)

        adds = [c for c in recorder.calls if c.method == "add_deck_labware"]
        assert {c.args["name"] for c in adds} == {first_plate.name, second_plate.name}
        deck = await lh.driver.get_deck_state(GetDeckStateRequest())
        names = [item.name for item in deck.labware]
        assert first_plate.name in names and second_plate.name in names

    @pytest.mark.asyncio
    async def test_same_name_different_instance_id_is_rejected(self) -> None:
        """One deck name belongs to one instance id: a second id arriving under
        the same name is a minted-name collision or a desynced driver world,
        and operating through it would touch the wrong plate."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        await lh.deck_world_layout()
        template = build.system.get_labware_template("sample_plate")
        first = await template.create_instance()
        await first.enter_record(build.system.labware_contents)
        impostor = PlateInstance(SimPlate(first.name), template_name=first.template_name, labware_type=first.labware_type)
        assert impostor.id != first.id and impostor.name == first.name
        mover = EXTERNAL_MOVER

        await lh._do_notify_placed(first, mover, target=WORKING_SLOT)
        with pytest.raises(DeckLabwareIdentityError):
            await lh._do_notify_placed(impostor, mover, target=ARM_TAUGHT_SITE)


class TestHooksGateOnTheActingMover:
    """The four hooks, driven directly, once per mover kind.

    These exist because the end-to-end tests above detect a broken gate only by
    the workflow FAILING (double-materialization raises "spot 0 already has a
    resource" before any driver-call assertion is reached). Detection by crash
    is not the same as pinning behaviour, so each hook is also asserted here
    where the assertion is always reachable.

    ``_deck_holds`` is forced False throughout, leaving the mover gate as the
    only thing that can suppress driver traffic.
    """

    async def _fixture(
        self,
    ) -> tuple[LiquidHandler, EmptyDeckStateDriver, LabwareInstance]:
        recorder = EmptyDeckStateDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        await lh.deck_world_layout()
        labware = await build.system.get_labware_template("sample_plate").create_instance()
        await labware.enter_record(build.system.labware_contents)
        recorder.calls.clear()
        return lh, recorder, labware

    @pytest.mark.asyncio
    async def test_gripper_is_wired_as_the_devices_own_mover(self) -> None:
        lh, _, _ = await self._fixture()
        assert lh.gripper is not None, (
            "the deck meshes >1 site, so build must wire a gripper and hand the "
            "device a reference; without it every hop reads as external")

    @pytest.mark.asyncio
    async def test_internal_hop_suppresses_all_four_hooks(self) -> None:
        lh, recorder, labware = await self._fixture()
        gripper = lh.gripper
        assert gripper is not None

        await lh._do_prepare_for_pick(labware, gripper, target=ARM_TAUGHT_SITE)
        await lh._do_prepare_for_place(labware, gripper, target=WORKING_SLOT)
        await lh._do_notify_picked(labware, gripper, target=ARM_TAUGHT_SITE)
        await lh._do_notify_placed(labware, gripper, target=WORKING_SLOT)

        emitted = [c.method for c in recorder.calls if c.method in DOOR_AND_WORLD_CALLS]
        assert emitted == [], (
            f"a hop by the device's OWN gripper must drive no door or world "
            f"traffic; got {emitted}")

    @pytest.mark.asyncio
    async def test_external_mover_drives_all_four_hooks(self) -> None:
        lh, recorder, labware = await self._fixture()
        mover = EXTERNAL_MOVER

        await lh._do_prepare_for_pick(labware, mover, target=ARM_TAUGHT_SITE)
        await lh._do_prepare_for_place(labware, mover, target=WORKING_SLOT)
        await lh._do_notify_picked(labware, mover, target=ARM_TAUGHT_SITE)
        await lh._do_notify_placed(labware, mover, target=WORKING_SLOT)

        emitted = [c.method for c in recorder.calls if c.method in DOOR_AND_WORLD_CALLS]
        assert emitted.count("open") == 2, (
            f"both prepare hooks must open for an external arm; got {emitted}")
        assert "close" in emitted, f"notify_picked must close; got {emitted}"
        assert "add_deck_labware" in emitted, (
            f"an external arrival must materialize the plate; got {emitted}")


class FailOncePlateMover(RecordingLiquidHandlerDriver):
    """Fails the first move_plate, then behaves. Models a transient driver
    fault, which is the case the operator RETRY path exists for."""

    def __init__(self, inner) -> None:
        super().__init__(inner)
        self.move_attempts = 0

    async def move_plate(self, request: MovePlateRequest) -> None:
        self.move_attempts += 1
        self.calls.append(RecordedCall(method="move_plate", args=request.model_dump()))
        if self.move_attempts == 1:
            raise RuntimeError("simulated transient gripper fault")


class TestFailedPlaceKeepsWhatTheRetryNeeds:
    """A failed place leaves the plate held, so the retry skips the pick and
    re-enters place directly. Everything that call needs must survive."""

    @pytest.mark.asyncio
    async def test_retry_reissues_the_move_from_the_same_source(self) -> None:
        recorder = FailOncePlateMover(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        gripper = lh.gripper
        assert gripper is not None
        labware = await build.system.get_labware_template("sample_plate").create_instance()

        await labware.enter_record(build.system.labware_contents)
        source = build.system.system_map.get_location(f"lh/{ARM_TAUGHT_SITE}")
        target = build.system.system_map.get_location(f"lh/{WORKING_SLOT}")
        source.initialize_labware(labware)
        await pick_and_record(build, gripper, source, labware)

        with pytest.raises(RuntimeError, match="transient gripper fault"):
            await gripper.place(target)

        assert gripper.labware is labware, (
            "a failed place leaves the plate in the jaws; the engine must keep modelling it")

        await gripper.place(target)

        moves = [c for c in recorder.calls if c.method == "move_plate"]
        assert len(moves) == 2, f"the failed attempt and the retry both issue; got {moves}"
        assert [m.args["from_position"] for m in moves] == [ARM_TAUGHT_SITE] * 2, (
            "the retry must re-issue from the ORIGINAL source; forgetting where the "
            f"pick came from sends from_position=None. got {[m.args for m in moves]}")

    @pytest.mark.asyncio
    async def test_place_without_a_recorded_source_raises_a_typed_error(self) -> None:
        """Reachable state routed to operator recovery, so it must raise rather
        than assert -- under -O a bare assert vanishes and from_position=None
        reaches the driver."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        gripper = lh.gripper
        assert gripper is not None
        labware = await build.system.get_labware_template("sample_plate").create_instance()

        await labware.enter_record(build.system.labware_contents)
        source = build.system.system_map.get_location(f"lh/{ARM_TAUGHT_SITE}")
        target = build.system.system_map.get_location(f"lh/{WORKING_SLOT}")
        source.initialize_labware(labware)
        await pick_and_record(build, gripper, source, labware)
        gripper._picked_from = None

        with pytest.raises(ValueError, match="picked from"):
            await gripper.place(target)
        assert not [c for c in recorder.calls if c.method == "move_plate"], (
            "no move_plate may be issued without a source position")


class TestPanicButtonReachesADeviceOwnedGripper:
    """The branch's regression: clear_all_labware reset holders from
    [devices, transporters], and `transporters` filters on the CONCRETE
    Transporter class, so a device-owned gripper was unreachable by every
    operator surface. A gripper wedged holding a plate stayed wedged."""

    @pytest.mark.asyncio
    async def test_clear_all_frees_a_gripper_left_holding_a_plate(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_single_plate_system(recorder)
        runtime = SystemRuntime(build.system, event_bus=build.event_bus)
        await runtime.start()
        try:
            lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
            gripper = lh.gripper
            assert gripper is not None
            labware = await build.system.get_labware_template("sample_plate").create_instance()
            await labware.enter_record(build.system.labware_contents)
            source = build.system.system_map.get_location(f"lh/{ARM_TAUGHT_SITE}")
            source.initialize_labware(labware)
            await pick_and_record(build, gripper, source, labware)
            assert gripper.labware is labware

            await runtime.labware.clear_all_labware(force=True)

            assert gripper.labware is None, (
                "the panic button must free a wedged gripper; otherwise every later "
                "move over it raises 'already contains labware' forever")
            assert gripper.picked_from_position_id is None, (
                "the recorded pick source must be cleared too, else a later place "
                "re-issues a move from a stale position")
        finally:
            await runtime.shutdown()


class TestReconcileWhenThereIsNoSiteToDeclareAHeldPlateAt:
    """Reconcile declares a plate in the device's own jaws at the site the hop
    began at, which is the only site either model can name for it. When there is
    no such site the plate leaves the driver's deck, and only a human can settle
    where it really is, so that is where the conflict is raised.

    The projection itself is pinned in
    ``test_refused_gripper_move_leaves_no_hold.py``.
    """

    async def _held_plate(
        self, recorder: RecordingLiquidHandlerDriver,
    ) -> tuple[SystemBuild, TransporterBase, LabwareInstance]:
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        gripper = next(m for m in build.system.movers if m.name == "lh/gripper")
        await lh.deck_world_layout()
        labware = await build.system.get_labware_template("sample_plate").create_instance()
        await labware.enter_record(build.system.labware_contents)
        source = build.system.system_map.get_location(f"lh/{ARM_TAUGHT_SITE}")
        source.initialize_labware(labware)
        await pick_and_record(build, gripper, source, labware)
        await source.notify_picked(labware, gripper)
        return build, gripper, labware

    @pytest.mark.asyncio
    async def test_reconcile_warns_when_nothing_recorded_where_the_pick_began(
        self, caplog,
    ) -> None:
        """A hold that outlived the process that made it: the jaws still have
        the plate and nothing left says where it came from."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build, gripper, _ = await self._held_plate(recorder)
        gripper._picked_from = None

        device_loc = build.system.system_map.get_location("lh")
        with caplog.at_level("WARNING", logger="orca"):
            await build.system.reconcile_lh_deck_occupancy(device_loc)

        assert any("gripper is holding" in r.getMessage() for r in caplog.records), (
            "a reconcile that drops a held plate from the driver world must at "
            f"least warn. records: {[r.getMessage() for r in caplog.records]}")

    @pytest.mark.asyncio
    async def test_reconcile_raises_a_conflict_for_the_held_plate(self) -> None:
        """A log line is not an operator surface. Once a hold survives a restart
        the mismatch is permanent, so it has to reach the operator the way a
        vanished site does."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build, gripper, labware = await self._held_plate(recorder)
        gripper._picked_from = None

        seen: list[DeckReconcileConflict] = []
        build.system.add_deck_reconcile_conflict_listener(seen.append)
        await build.system.reconcile_lh_deck_occupancy(
            build.system.system_map.get_location("lh")
        )

        assert [c.labware_id for c in seen] == [labware.id]
        assert seen[0].position_id == gripper.gripper_location.position_id

    @pytest.mark.asyncio
    async def test_the_site_the_hop_began_at_is_not_declared_twice(self) -> None:
        """Another plate landed on the site this one left. One name per site, so
        the held plate is the one that cannot be declared."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build, _, labware = await self._held_plate(recorder)
        newcomer = await build.system.get_labware_template("sample_plate").create_instance()
        await newcomer.enter_record(build.system.labware_contents)
        source = build.system.system_map.get_location(f"lh/{ARM_TAUGHT_SITE}")
        await source.place_labware(newcomer)

        seen: list[DeckReconcileConflict] = []
        build.system.add_deck_reconcile_conflict_listener(seen.append)
        await build.system.reconcile_lh_deck_occupancy(
            build.system.system_map.get_location("lh")
        )

        assert [c.labware_id for c in seen] == [labware.id]
        pushed = [c for c in recorder.calls if c.method == "reconcile_deck_occupancy"]
        declared = [r["name"] for r in pushed[-1].args["resources"]]
        assert declared == [newcomer.name], (
            f"the site's real occupant is the one declared; got {declared}")


class TestExternalControlCoversLiquidHandlerDeckSites:
    """Holding a device under external control must stop the engine touching
    it. The resolver understood LabwareStagingBridge and Device, but a flat
    deck site's resource is DeviceDeckSite -- neither -- so it returned None
    every time and an arm could pick off a deck the operator had taken."""

    @pytest.mark.asyncio
    async def test_a_held_liquid_handler_blocks_its_deck_sites(self) -> None:
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        site = build.system.system_map.get_location(f"lh/{ARM_TAUGHT_SITE}")

        assert device_under_external_control(site) is None, "not held yet"

        lh.take_external_control()
        try:
            assert device_under_external_control(site) is lh, (
                "a deck site must resolve to its owning device, or the interlock "
                "silently passes and an arm reaches into a device the operator holds")
        finally:
            lh.release_external_control()

        assert device_under_external_control(site) is None, "release must clear it"

    @pytest.mark.asyncio
    async def test_a_held_device_also_holds_its_own_gripper(self) -> None:
        """The gripper is the device's arm: taking the device must take it too,
        and in_use must agree with under_external_control on the same object."""
        recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        gripper = lh.gripper
        assert gripper is not None
        assert not gripper.under_external_control and not gripper.in_use

        lh.take_external_control()
        try:
            assert gripper.under_external_control, (
                "the paired device's interlock must cover its own gripper")
            assert gripper.in_use, (
                "in_use must read the property, not the private field, or the object "
                "reports itself free while under external control")
        finally:
            lh.release_external_control()

        gripper.take_external_control()
        try:
            assert gripper.under_external_control, (
                "the gripper must still be holdable in its own right")
        finally:
            gripper.release_external_control()
        assert not gripper.under_external_control


class TestGripperSerializesAgainstTheDeviceDriver:
    """The gripper drives the device's own driver, so its move_plate must not
    overlap action dispatch or reconcile, both of which hold device.lock."""

    @pytest.mark.asyncio
    async def test_move_plate_runs_under_the_device_lock(self) -> None:
        observed: list[bool] = []

        class LockObservingDriver(RecordingLiquidHandlerDriver):
            async def move_plate(self, request: MovePlateRequest) -> None:
                observed.append(lh.lock.locked())
                self.calls.append(RecordedCall(method="move_plate", args=request.model_dump()))

        recorder = LockObservingDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
        build = await build_single_plate_system(recorder)
        lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
        gripper = lh.gripper
        assert gripper is not None
        labware = await build.system.get_labware_template("sample_plate").create_instance()

        await labware.enter_record(build.system.labware_contents)
        source = build.system.system_map.get_location(f"lh/{ARM_TAUGHT_SITE}")
        target = build.system.system_map.get_location(f"lh/{WORKING_SLOT}")
        source.initialize_labware(labware)
        await pick_and_record(build, gripper, source, labware)
        await gripper.place(target)

        assert observed == [True], (
            "move_plate must hold the device lock; without it a gripper hop can "
            f"overlap a pipetting action on the same physical device. observed={observed}")
