"""A driver world is laid out once, and that layout is what it holds.

The question "what deck is this driver laid out with" has one owner: the world
itself, filled in the first time it is asked. The layout store is where the
answer is DECLARED, so it is read to lay a bare world out and never again.

Two ways the old shape bit, in the order they matter:

1. A single operation read the store twice through two objects and used both
   answers: the carrier skeleton came from one read and the occupancy placed
   onto it from the other, with nothing checking they described the same deck.
2. Editing a layout under a running liquid handler wedged it permanently. The
   world held the old layout, every operation resolved the new one, and the
   two never agreed again -- while the edit's own prompt promises the running
   deck is untouched until the next rebuild.

Both are the same defect: two sources for one fact. There is now one call that
answers and lays out together, so no caller can hold a layout the driver does
not have.
"""

from cheshire_drivers import (
    DeckLayoutConfig, DeckResourceConfig, RecordingLiquidHandlerDriver,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver

from orca.devices.devices import LiquidHandler
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.interfaces import IDeckLayoutStore
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from tests.test_helpers import PairDriverFactory, seeded


def _layout(name: str, rail: int) -> DeckLayoutConfig:
    return DeckLayoutConfig(
        deck_type="STARlet",
        resources=[
            DeckResourceConfig(name=name, catalog_ref="PLT_CAR_L5AC_A00", rail=rail),
        ],
    )


_RAIL_7 = _layout("carrier-7", 7)
_RAIL_9 = _layout("carrier-9", 9)


def _recorded_lh() -> tuple[LiquidHandler, RecordingLiquidHandlerDriver, IDeckLayoutStore]:
    live = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    sim = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    stores = InMemoryRuntimeStoreFactory()
    store = stores.deck_layouts("lh", seed={"default": _RAIL_7})
    with use_device_factory(PairDriverFactory(live, sim)):
        lh = LiquidHandler("lh", deck_layout_store=store, deck_layout="default")
    return lh, live, store


def _configures(driver: RecordingLiquidHandlerDriver) -> list[str]:
    return [
        call.args["config"]["resources"][0]["name"]
        for call in driver.calls
        if call.method == "configure_deck"
    ]


class TestTheFirstAskLaysTheDeckOut:
    async def test_the_declared_layout_reaches_the_driver(self) -> None:
        lh, live, _store = _recorded_lh()
        with seeded(WorkflowRunMode.LIVE):
            answer = await lh.deck_world_layout()
        assert answer == (_RAIL_7, True)
        assert _configures(live) == ["carrier-7"]

    async def test_asking_again_does_not_lay_it_out_twice(self) -> None:
        lh, live, _store = _recorded_lh()
        with seeded(WorkflowRunMode.LIVE):
            await lh.deck_world_layout()
            answer = await lh.deck_world_layout()
        assert answer == (_RAIL_7, False)
        assert _configures(live) == ["carrier-7"]

    async def test_a_device_declaring_no_layout_answers_nothing(self) -> None:
        """Distinct from "already laid out". Conflating the two is what let a
        narrow write land on a deck nothing had configured."""
        lh = LiquidHandler("lh")
        with seeded(WorkflowRunMode.PURE_SIM):
            assert await lh.deck_world_layout() is None


class TestEditingALayoutDoesNotWedgeTheRunningDeck:
    """The edit's own prompt promises the running liquid handler keeps its
    layout until the next rebuild. It has to be true, and it has to stay
    usable: a world that answers with a deck its driver does not hold cannot
    place anything, ever again."""

    async def test_the_world_keeps_the_layout_it_was_laid_out_with(self) -> None:
        lh, live, store = _recorded_lh()
        with seeded(WorkflowRunMode.LIVE):
            await lh.deck_world_layout()
            await store.update("default", _RAIL_9)
            answer = await lh.deck_world_layout()
        assert answer == (_RAIL_7, False), (
            "the driver still holds carrier-7; answering carrier-9 would place "
            "labware at a carrier that is not there"
        )
        assert _configures(live) == ["carrier-7"]

    async def test_the_edit_lands_on_the_next_rebuilt_world(self) -> None:
        """Invalidating is what a rebuild does. The new layout takes effect
        then, which is the contract the edit prompt states."""
        lh, live, store = _recorded_lh()
        with seeded(WorkflowRunMode.LIVE):
            await lh.deck_world_layout()
            await store.update("default", _RAIL_9)
            await lh.invalidate_deck_world()
            answer = await lh.deck_world_layout()
        assert answer == (_RAIL_9, True)
        assert _configures(live) == ["carrier-7", "carrier-9"]


class TestEachWorldIsLaidOutSeparately:
    async def test_a_sim_layout_does_not_satisfy_the_live_instrument(self) -> None:
        lh, live, _store = _recorded_lh()
        with seeded(WorkflowRunMode.PURE_SIM):
            await lh.deck_world_layout()
        assert _configures(live) == []
        with seeded(WorkflowRunMode.LIVE):
            assert await lh.deck_world_layout() == (_RAIL_7, True)
        assert _configures(live) == ["carrier-7"]
