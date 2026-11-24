"""What a deck compare sends to the driver, and that it never writes the ledger.

A world only behind the record is rebuilt first, with the rebuild its next command would send, and then read. A world
in doubt or up to date is read with nothing sent before the read. Each case runs on an Opentrons Flex deck and on a
Hamilton STARlet deck, each world on a simulator of its own, through the real device path.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig
from cheshire_drivers.liquid_handler_models import AddDeckLabwareRequest, GetDeckStateRequest
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from cheshire_drivers.sims import RecordedCall
from pydantic import JsonValue

import orca.orca as orca
from orca.devices.devices import LiquidHandler
from orca.events.event_bus import EventBus
from orca.gateway.controller.exceptions import CommandExecutionError
from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import OUT_OF_RUN_BASE, WorkflowRunMode, current_run_mode, mode_scope
from orca.operations.device import InitializeDeviceOperation, ReconcileDeckOperation
from orca.operations.device_models import InitializeDeviceRequest, ReconcileDeckRequest
from orca.runtime.runtime_interface import ISystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.system.system_interface import DeckConflictReason
from tests.device_modes import set_to
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import real_device_path, the_real_device_path
from tests.recording_handler import RecordingHandler
from tests.setup_package import package_stores, with_deck_layouts, write_deck_layouts

pytestmark = [pytest.mark.out_of_run, pytest.mark.asyncio, pytest.mark.timeout(120)]

PLATE_TYPE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"
STRAY = "stray_plate"
LH = "lh"
REBUILD = ["reconcile_deck_occupancy"]
"""What rebuilding a handler's world sends: its whole occupancy from the record, in one command."""
SIM, LIVE, CHECK_RUN =WorkflowRunMode.SIM, WorkflowRunMode.LIVE, WorkflowRunMode.CHECK_RUN


@dataclass(frozen=True)
class _Deck:
    layout: DeckLayoutConfig
    plate_site: str
    free_site: str


FLEX = _Deck(DeckLayoutConfig(deck_type="FlexDeck", resources=[]), "C2-slot", "C3-slot")
STARLET = _Deck(
    DeckLayoutConfig(deck_type="STARlet", resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
    ]),
    "carrier-7-0", "carrier-7-1",
)
_EACH_DECK = pytest.mark.parametrize("deck", [FLEX, STARLET], ids=["opentrons_flex", "hamilton_starlet"])


class _OneRecordedDeckPerWorld(RecordingHandler):
    """A liquid handler whose every run mode has a simulator of its own, each recording what it is sent."""

    def __init__(self) -> None:
        self.simulators = {mode: ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8) for mode in WorkflowRunMode}
        self.worlds = {mode: RecordingHandler(simulator) for mode, simulator in self.simulators.items()}
        self.calls = []

    @property
    def _inner(self) -> RecordingHandler:
        return self.worlds[current_run_mode.get(OUT_OF_RUN_BASE)]

    @property
    def _handler(self) -> RecordingHandler:
        return self._inner

    def sent_to(self, mode: WorkflowRunMode) -> list[str]:
        return [call.method for call in self.worlds[mode].calls]

    def forget_what_was_sent(self) -> None:
        for world in self.worlds.values():
            world.calls.clear()


@dataclass
class _Bench:
    runtime: ISystemRuntime
    lh: LiquidHandler
    driver: _OneRecordedDeckPerWorld
    plate_id: str
    plate_name: str
    deck: _Deck

    async def compare(self, mode: WorkflowRunMode) -> list[tuple[str | None, DeckConflictReason]]:
        with mode_scope(mode):
            comparison = await self.runtime.devices.compare_deck(LH)
        return [(d.labware_name, d.reason) for d in comparison.disagreements]

    async def read_by_hand(self, mode: WorkflowRunMode) -> None:
        """An operator's get_deck_state, a normal command, which rebuilds a marked world first."""
        with mode_scope(mode):
            await OperatorDeviceCommands(the_real_device_path().gateway, lambda: self.runtime).run_named(
                LH, "get_deck_state", {},
            )

    async def put_a_stray_plate_on(self, mode: WorkflowRunMode) -> None:
        """A plate the driver holds and the record never heard of, written straight to that world's simulator."""
        await self.driver.simulators[mode].add_deck_labware(
            AddDeckLabwareRequest(name=STRAY, catalog_ref=PLATE_TYPE, at=self.deck.free_site),
        )

    async def held_by(self, mode: WorkflowRunMode) -> set[str]:
        """What the world's simulator holds, read without sending it a command."""
        state = await self.driver.simulators[mode].get_deck_state(GetDeckStateRequest())
        return {item.name for item in state.labware}

    async def ledger(self) -> tuple[list[tuple[str, str]], list[tuple[str, str | None]], list[str]]:
        system = self.runtime.system
        location = system.system_map.get_resource_location(LH)
        return (
            sorted((lw.id, lw.name) for lw in system.labwares),
            [
                (site.position_id, None if site.labware is None else site.labware.id)
                for site in system.system_map.sites_of(location.owner_mutex_id or location.position_id)
            ],
            sorted(repr(snapshot) for snapshot in await self.runtime.labware.list_all()),
        )

    def sent_with_args(self, mode: WorkflowRunMode) -> list[tuple[str, str]]:
        """What the world was sent, with this bench's plate name made alike across benches."""
        return [_plain(call, self.plate_name) for call in self.driver.worlds[mode].calls]


def _plain(call: RecordedCall, plate_name: str) -> tuple[str, str]:
    args: dict[str, JsonValue] = call.args
    return call.method, repr(args).replace(plate_name, "plate")


@asynccontextmanager
async def _bench(deck: _Deck) -> AsyncIterator[_Bench]:
    """A handler set to live, with one plate on record on its deck, held by its LIVE, SIM and CHECK_RUN worlds."""
    stores = package_stores()
    driver = _OneRecordedDeckPerWorld()
    the_real_device_path().simulate_with(LH, driver)
    lh = with_deck_layouts(LiquidHandler(LH, deck_layout="default"), write_deck_layouts(stores, LH, {"default": deck.layout}))
    set_to(lh, LH, DeviceMode.LIVE)
    build = await orca.build_system(
        name="Deck Compare", topology=Topology(locations={LH: lh}, transporters=[]), stores=stores,
        labwares=[PlateTemplate("sample_plate", labware_type=PLATE_TYPE)],
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        registered = await runtime.labware.register("sample_plate", location=f"{LH}/{deck.plate_site}", confirm=True)
        instance = next(lw for lw in build.system.labwares if lw.id == registered.id)
        site = build.system.system_map.resolve_placement_location(f"{LH}/{deck.plate_site}")
        for mode in (SIM, CHECK_RUN):
            with mode_scope(mode):
                await build.system.project_labware_on_lh_decks(instance, site)
        bench = _Bench(runtime, lh, driver, registered.id, instance.name, deck)
        for mode in WorkflowRunMode:
            assert instance.name in await bench.held_by(mode), f"the {mode} world does not hold the plate"
        driver.forget_what_was_sent()
        yield bench
    finally:
        await runtime.shutdown()


async def _compared(bench: _Bench, mode: WorkflowRunMode) -> list[tuple[str | None, DeckConflictReason]]:
    before = await bench.ledger()
    reported = await bench.compare(mode)
    assert await bench.ledger() == before, "the compare wrote the ledger"
    return reported


async def _discharged(bench: _Bench) -> None:
    """The plate is discharged with LIVE in force: the SIM and CHECK_RUN worlds still hold it."""
    await bench.runtime.labware.discharge_labware(bench.plate_id, force=True, confirm=True)
    assert bench.plate_name in await bench.held_by(SIM)
    bench.driver.forget_what_was_sent()


@_EACH_DECK
async def test_compare_in_another_run_mode_after_a_discharge_reports_nothing_and_sends_the_rebuild_the_next_command_would(
    deck: _Deck,
) -> None:
    async with _bench(deck) as bench:
        await _discharged(bench)

        assert await _compared(bench, SIM) == [], "the compare reported a plate the record removed in another mode"
        by_the_compare = bench.sent_with_args(SIM)
        assert bench.driver.sent_to(LIVE) == [] and bench.driver.sent_to(CHECK_RUN) == []

    async with real_device_path(), _bench(deck) as twin:
        await _discharged(twin)
        await twin.read_by_hand(SIM)
        by_the_next_command = twin.sent_with_args(SIM)

    assert [method for method, _ in by_the_compare] == REBUILD + ["get_deck_state"]
    assert by_the_compare == by_the_next_command, "the compare's rebuild is not the one the next command sends"


@_EACH_DECK
@pytest.mark.parametrize("mode", [SIM, CHECK_RUN], ids=["sim", "check_run"])
async def test_compare_with_a_run_mode_in_force_after_a_discharge_in_live_reports_nothing(
    deck: _Deck, mode: WorkflowRunMode,
) -> None:
    async with _bench(deck) as bench:
        await _discharged(bench)

        assert await _compared(bench, mode) == []
        assert bench.driver.sent_to(mode) == REBUILD + ["get_deck_state"]
        assert bench.plate_name not in await bench.held_by(mode)


async def _reconcile(bench: _Bench) -> None:
    await bench.runtime.devices.reconcile_deck(LH, confirm=True)


async def _initialize(bench: _Bench) -> None:
    await bench.runtime.devices.after_lifecycle(LH, "initialize")


async def _disconnect(bench: _Bench) -> None:
    await bench.runtime.devices.after_lifecycle(LH, "disconnect")


@_EACH_DECK
@pytest.mark.parametrize("command", [_reconcile, _initialize, _disconnect], ids=["reconcile", "initialize", "disconnect"])
async def test_a_reconcile_or_lifecycle_command_in_live_leaves_the_sim_world_only_behind_the_record(
    deck: _Deck, command: Callable[[_Bench], Awaitable[None]],
) -> None:
    """The command reaches only the LIVE driver, so it puts no other world in doubt."""
    async with _bench(deck) as bench:
        await command(bench)
        await _discharged(bench)

        assert await _compared(bench, SIM) == [], "the compare read a SIM world the LIVE command put in doubt"
        assert bench.driver.sent_to(SIM) == REBUILD + ["get_deck_state"]


def _operator_commands(bench: _Bench) -> OperatorDeviceCommands:
    return OperatorDeviceCommands(the_real_device_path().gateway, lambda: bench.runtime)


async def _initialize_by_route(bench: _Bench) -> None:
    await InitializeDeviceOperation(runtime=bench.runtime, devices=_operator_commands(bench)).run(
        InitializeDeviceRequest(device_id=LH),
    )


async def _reconcile_by_route(bench: _Bench) -> None:
    await ReconcileDeckOperation(runtime=bench.runtime, devices=_operator_commands(bench)).run(
        ReconcileDeckRequest(device_id=LH),
    )


@_EACH_DECK
@pytest.mark.parametrize("command", [_initialize_by_route, _reconcile_by_route], ids=["initialize", "reconcile"])
async def test_the_deck_work_after_an_operator_command_lands_on_the_world_the_command_was_sent_to(
    deck: _Deck, command: Callable[[_Bench], Awaitable[None]],
) -> None:
    """The run mode is read once: a run starting or ending mid-command cannot send the deck work to another world."""
    async with _bench(deck) as bench:
        with patch.object(bench.runtime, "operator_base_mode", return_value=SIM):
            await command(bench)

        assert "configure_deck" in bench.driver.sent_to(SIM), "the SIM world the command reached was not laid out again"
        assert "configure_deck" not in bench.driver.sent_to(LIVE), "the deck work went to a world the command missed"


@_EACH_DECK
async def test_after_the_compare_rebuilt_a_world_the_next_command_sends_no_second_rebuild(deck: _Deck) -> None:
    async with _bench(deck) as bench:
        await _discharged(bench)
        await _compared(bench, SIM)
        bench.driver.forget_what_was_sent()

        await bench.read_by_hand(SIM)

        assert bench.driver.sent_to(SIM) == ["get_deck_state"]


@_EACH_DECK
async def test_a_world_in_doubt_is_read_as_it_stands_with_nothing_sent_first_and_its_stray_plate_is_reported(
    deck: _Deck,
) -> None:
    async with _bench(deck) as bench:
        await bench.put_a_stray_plate_on(LIVE)
        await bench.runtime.system.device_worlds.mark_current_world(LH)

        assert await _compared(bench, LIVE) == [(STRAY, DeckConflictReason.UNKNOWN_TO_LEDGER)]
        assert bench.driver.sent_to(LIVE) == ["get_deck_state"]


@_EACH_DECK
async def test_a_world_in_doubt_stays_in_doubt_when_the_record_then_changes(deck: _Deck) -> None:
    async with _bench(deck) as bench:
        await bench.put_a_stray_plate_on(LIVE)
        await bench.runtime.system.device_worlds.mark_current_world(LH)
        with mode_scope(SIM):
            await bench.runtime.system.device_worlds.mark_other_worlds()

        assert await _compared(bench, LIVE) == [(STRAY, DeckConflictReason.UNKNOWN_TO_LEDGER)], (
            "a later record change let the compare rebuild a world in doubt and hide the stray plate"
        )
        assert bench.driver.sent_to(LIVE) == ["get_deck_state"]


@_EACH_DECK
async def test_a_world_up_to_date_is_read_with_nothing_sent_first_and_its_stray_plate_is_reported(deck: _Deck) -> None:
    async with _bench(deck) as bench:
        await bench.runtime.system.device_worlds.bring_up_to_date(LH)
        await bench.put_a_stray_plate_on(LIVE)
        bench.driver.forget_what_was_sent()

        assert await _compared(bench, LIVE) == [(STRAY, DeckConflictReason.UNKNOWN_TO_LEDGER)]
        assert bench.driver.sent_to(LIVE) == ["get_deck_state"]


@_EACH_DECK
@pytest.mark.parametrize("mode", [LIVE, SIM, CHECK_RUN], ids=["live", "sim", "check_run"])
async def test_after_clear_all_a_compare_in_any_run_mode_reports_nothing(deck: _Deck, mode: WorkflowRunMode) -> None:
    async with _bench(deck) as bench:
        await bench.runtime.labware.clear_all_labware(force=True, confirm=True)

        assert await _compared(bench, mode) == []


@_EACH_DECK
async def test_a_failed_rebuild_fails_the_compare_as_a_failed_read_does_and_leaves_the_world_in_doubt(
    deck: _Deck,
) -> None:
    """A rebuild command that fails may have left the deck half written, as any failed command may."""
    async with _bench(deck) as bench:
        await _discharged(bench)
        before = await bench.ledger()
        world = bench.driver.worlds[SIM]
        with patch.object(world, REBUILD[-1], side_effect=RuntimeError("the deck write failed")):
            with pytest.raises(CommandExecutionError, match="the deck write failed"):
                await bench.compare(SIM)
        with patch.object(world, "get_deck_state", side_effect=RuntimeError("the deck read failed")):
            with pytest.raises(CommandExecutionError, match="the deck read failed"):
                await bench.compare(SIM)
        assert await bench.ledger() == before, "a failed compare wrote the ledger"
        bench.driver.forget_what_was_sent()

        assert await _compared(bench, SIM) == [(bench.plate_name, DeckConflictReason.UNKNOWN_TO_LEDGER)]
        assert bench.driver.sent_to(SIM) == ["get_deck_state"], "a world in doubt was rebuilt before the read"


@_EACH_DECK
async def test_the_compare_waits_while_a_command_holds_the_device_and_rebuilds_only_after_it_lets_go(
    deck: _Deck,
) -> None:
    async with _bench(deck) as bench:
        await _discharged(bench)
        holding, done = asyncio.Event(), asyncio.Event()

        async def another_command() -> None:
            async with bench.lh.lock.held_for("another command"):
                holding.set()
                await done.wait()

        holder = asyncio.create_task(another_command())
        await holding.wait()
        lock = bench.lh.lock
        hold_for, asked = lock.held_for, asyncio.Event()

        def asking(purpose: str) -> AbstractAsyncContextManager[None]:
            asked.set()
            return hold_for(purpose)

        with patch.object(lock, "held_for", asking):
            compare = asyncio.create_task(_compared(bench, SIM))
            await asyncio.wait({compare, asyncio.create_task(asked.wait())}, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(0)
        assert not compare.done(), "the compare did not wait for the device"
        assert bench.driver.sent_to(SIM) == [], "the compare sent to a device another command holds"

        done.set()
        await holder
        assert await compare == []
        assert bench.driver.sent_to(SIM) == REBUILD + ["get_deck_state"]
