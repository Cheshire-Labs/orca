"""Tests for IDeckLayoutStore impls (file store + service) and
LiquidHandler.resolve_deck_config_async().

The file is the store, so reads reconstruct an equal config rather than
returning the same object.
"""

import pytest
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig, DeckResourceConfig


from orca.devices.devices import LiquidHandler
from orca.runtime.deck_layout_service import (
    DeckLayoutService,
)
from orca.runtime.setup_file_stores import DeckLayoutFileStore
from tests.setup_package import deck_layout_file, fresh_root, with_deck_layouts


def _config(deck_type: str = "STARlet") -> DeckLayoutConfig:
    return DeckLayoutConfig(
        deck_type=deck_type,
        resources=[
            DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        ],
    )


def _store() -> DeckLayoutFileStore:
    return DeckLayoutFileStore(fresh_root() / "deck_layouts" / "lh.json")


class TestDeckLayoutFileStoreCrud:
    """Raw CRUD on the file store: no copy is kept, so every read goes back to the file."""

    async def test_get_returns_none_for_unknown(self) -> None:
        assert await _store().get("missing") is None

    async def test_add_then_get_roundtrips(self) -> None:
        store = _store()
        cfg = _config("STARlet")
        await store.add("default", cfg)
        loaded = await store.get("default")
        assert loaded == cfg

    async def test_list_orders_by_name(self) -> None:
        store = _store()
        await store.add("b", _config())
        await store.add("a", _config())
        assert [name for name, _ in await store.list()] == ["a", "b"]

    async def test_add_duplicate_name_raises(self) -> None:
        store = _store()
        await store.add("dup", _config())
        with pytest.raises(ValueError, match="already registered"):
            await store.add("dup", _config())

    async def test_update_replaces_value(self) -> None:
        store = _store()
        await store.add("default", _config("STARlet"))
        await store.update("default", _config("STAR"))
        loaded = await store.get("default")
        assert loaded is not None
        assert loaded.deck_type == "STAR"

    async def test_update_unknown_raises(self) -> None:
        with pytest.raises(KeyError, match="not found"):
            await _store().update("ghost", _config())

    async def test_delete_returns_true_when_present(self) -> None:
        store = _store()
        await store.add("default", _config())
        assert await store.delete("default") is True
        assert await store.get("default") is None

    async def test_delete_returns_false_when_absent(self) -> None:
        assert await _store().delete("ghost") is False


class TestDeckLayoutFileDurability:
    """The file, not the store object, holds the truth: a fresh store on the
    same file sees what a prior store wrote."""

    async def test_a_fresh_store_reads_what_another_wrote(self, tmp_path) -> None:
        writer = DeckLayoutFileStore(tmp_path / "lh.json")
        await writer.add("default", _config("STARlet"))

        loaded = await DeckLayoutFileStore(tmp_path / "lh.json").get("default")
        assert loaded is not None
        assert loaded.deck_type == "STARlet"


class TestDeckLayoutServiceCrud:
    """The service is the single-lock + lifecycle layer over a raw store."""

    async def test_add_then_get_roundtrips(self) -> None:
        service = DeckLayoutService(_store())
        cfg = _config("STARlet")
        await service.add("default", cfg)
        assert await service.get("default") == cfg

    async def test_list_returns_all(self) -> None:
        service = DeckLayoutService(_store())
        await service.add("a", _config())
        await service.add("b", _config())
        assert {name for name, _ in await service.list()} == {"a", "b"}

    async def test_add_duplicate_raises(self) -> None:
        service = DeckLayoutService(_store())
        await service.add("dup", _config())
        with pytest.raises(ValueError, match="already registered"):
            await service.add("dup", _config())

    async def test_update_unknown_raises(self) -> None:
        service = DeckLayoutService(_store())
        with pytest.raises(KeyError, match="not found"):
            await service.update("ghost", _config())

    async def test_delete_removes(self) -> None:
        service = DeckLayoutService(_store())
        await service.add("default", _config())
        assert await service.delete("default") is True
        assert await service.get("default") is None


class TestLiquidHandlerResolveDeckConfig:

    async def test_no_deck_layout_yields_none_before_the_build_attaches_one(self) -> None:
        lh = LiquidHandler("lh")
        assert await lh.resolve_deck_config_async() is None

    async def test_a_handler_the_build_has_not_attached_refuses_to_read_its_layouts(self) -> None:
        lh = LiquidHandler("lh", sim=True, deck_layout="default")
        with pytest.raises(RuntimeError, match="no deck layouts attached"):
            await lh.resolve_deck_config_async()

    async def test_resolves_named_layout(self) -> None:
        cfg = _config("STARlet")
        store = deck_layout_file({"default": cfg})
        lh = with_deck_layouts(LiquidHandler(
            "lh",
            sim=True,
            deck_layout="default",
        ), store)
        resolved = await lh.resolve_deck_config_async()
        assert resolved == cfg

    async def test_unknown_layout_yields_none(self) -> None:
        store = deck_layout_file({"a": _config()})
        lh = with_deck_layouts(LiquidHandler(
            "lh",
            sim=True,
            deck_layout="b",
        ), store)
        assert await lh.resolve_deck_config_async() is None

    async def test_no_deck_layout_yields_none_even_with_store(self) -> None:
        store = deck_layout_file({"a": _config()})
        lh = with_deck_layouts(LiquidHandler(
            "lh",
            sim=True,
        ), store)
        assert await lh.resolve_deck_config_async() is None


class TestLiquidHandlerResolveDeckConfigAsync:
    """The runtime resolver awaits the store; the database is authoritative.

    `resolve_deck_config_async` reads the live store (no
    snapshot); both the System builder and runtime callers use it.
    """

    async def test_resolves_named_layout(self) -> None:
        cfg = _config("STARlet")
        store = deck_layout_file({"default": cfg})
        lh = with_deck_layouts(LiquidHandler(
            "lh",
            sim=True,
            deck_layout="default",
        ), store)
        resolved = await lh.resolve_deck_config_async()
        assert resolved == cfg

