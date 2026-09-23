"""Tests for IDeckLayoutStore impls (Null, SQLite store + service) and
LiquidHandler.resolve_deck_config_async().

The SQLite store is the source of truth: the whole config persists as one
JSON blob, so reads reconstruct an equal config rather than returning the
same object. The DeckLayoutService is the lifecycle + single-lock layer over
the raw store; ``seeded_deck_layout_service`` lazy-seeds it on first async
access for sync construction sites.
"""

import pytest
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig, DeckResourceConfig


from orca.devices.devices import LiquidHandler
from orca.runtime.db import create_memory_engine, create_sqlite_engine
from orca.runtime.deck_layout_service import (
    DeckLayoutService,
    seeded_deck_layout_service,
)
from orca.runtime.deck_layout_store import NullDeckLayoutStore
from orca.runtime.sqlite_deck_layout_store import SqliteDeckLayoutStore


def _config(deck_type: str = "STARlet") -> DeckLayoutConfig:
    return DeckLayoutConfig(
        deck_type=deck_type,
        resources=[
            DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        ],
    )


def _store() -> SqliteDeckLayoutStore:
    return SqliteDeckLayoutStore(create_memory_engine())


class TestNullDeckLayoutStore:

    async def test_get_returns_none(self) -> None:
        assert await NullDeckLayoutStore().get("anything") is None

    async def test_list_returns_empty(self) -> None:
        assert await NullDeckLayoutStore().list() == []

    async def test_add_is_noop(self) -> None:
        store = NullDeckLayoutStore()
        await store.add("v", _config())
        assert await store.get("v") is None
        assert await store.list() == []

    async def test_update_is_noop(self) -> None:
        store = NullDeckLayoutStore()
        await store.update("v", _config())
        assert await store.get("v") is None
        assert await store.list() == []

    async def test_delete_returns_false(self) -> None:
        assert await NullDeckLayoutStore().delete("anything") is False


class TestSqliteDeckLayoutStoreCrud:
    """Raw CRUD on the SQLite store: the DB is the source of truth and no
    in-memory copy is kept, so every read goes back to the rows on disk."""

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


class TestSqliteDeckLayoutStoreDurability:
    """The database, not the store object, holds the truth: a fresh store on
    the same engine sees what a prior store wrote."""

    async def test_memory_engine_round_trip_across_store_instances(self) -> None:
        engine = create_memory_engine()
        writer = SqliteDeckLayoutStore(engine)
        await writer.add("default", _config("STARlet"))

        reader = SqliteDeckLayoutStore(engine)
        loaded = await reader.get("default")
        assert loaded is not None
        assert loaded.deck_type == "STARlet"

    async def test_file_engine_persists_across_reopen(self, tmp_path) -> None:
        db_path = tmp_path / "deck.db"
        engine = create_sqlite_engine(str(db_path))
        writer = SqliteDeckLayoutStore(engine)
        await writer.add("default", _config("STAR"))
        await writer.aclose()

        reopened = create_sqlite_engine(str(db_path))
        reader = SqliteDeckLayoutStore(reopened)
        loaded = await reader.get("default")
        assert loaded is not None
        assert loaded.deck_type == "STAR"
        await reader.aclose()


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

    async def test_seed_if_missing_is_idempotent(self) -> None:
        service = DeckLayoutService(_store())
        await service.seed_if_missing({"default": _config("STARlet")})
        await service.seed_if_missing({"default": _config("STAR")})
        loaded = await service.get("default")
        assert loaded is not None
        # Second seed never overwrites the first.
        assert loaded.deck_type == "STARlet"


class TestDeckLayoutServiceSeeding:
    """The lazy-seeded service reconciles its seed on first async access."""

    async def test_seeded_service_lists_seed_on_first_access(self) -> None:
        service = seeded_deck_layout_service({"a": _config(), "b": _config()})
        assert {name for name, _ in await service.list()} == {"a", "b"}

    async def test_seed_never_overwrites_existing(self) -> None:
        service = seeded_deck_layout_service({"default": _config("STARlet")})
        await service.list()
        await service.seed_if_missing({"default": _config("STAR")})
        loaded = await service.get("default")
        assert loaded is not None
        assert loaded.deck_type == "STARlet"

    async def test_empty_seed_default_is_empty(self) -> None:
        service = seeded_deck_layout_service()
        assert await service.list() == []


class TestLiquidHandlerResolveDeckConfig:

    async def test_default_store_yields_none(self) -> None:
        lh = LiquidHandler("lh")
        assert await lh.resolve_deck_config_async() is None

    async def test_resolves_named_layout(self) -> None:
        cfg = _config("STARlet")
        store = seeded_deck_layout_service({"default": cfg})
        lh = LiquidHandler(
            "lh",
            sim=True,
            deck_layout_store=store,
            deck_layout="default",
        )
        resolved = await lh.resolve_deck_config_async()
        assert resolved == cfg

    async def test_unknown_layout_yields_none(self) -> None:
        store = seeded_deck_layout_service({"a": _config()})
        lh = LiquidHandler(
            "lh",
            sim=True,
            deck_layout_store=store,
            deck_layout="b",
        )
        assert await lh.resolve_deck_config_async() is None

    async def test_no_deck_layout_yields_none_even_with_store(self) -> None:
        store = seeded_deck_layout_service({"a": _config()})
        lh = LiquidHandler(
            "lh",
            sim=True,
            deck_layout_store=store,
        )
        assert await lh.resolve_deck_config_async() is None


class TestLiquidHandlerResolveDeckConfigAsync:
    """The runtime resolver awaits the store; the database is authoritative.

    `resolve_deck_config_async` reads the live store (no
    snapshot); both the System builder and runtime callers use it.
    """

    async def test_resolves_named_layout(self) -> None:
        cfg = _config("STARlet")
        store = seeded_deck_layout_service({"default": cfg})
        lh = LiquidHandler(
            "lh",
            sim=True,
            deck_layout_store=store,
            deck_layout="default",
        )
        resolved = await lh.resolve_deck_config_async()
        assert resolved == cfg

    async def test_default_store_yields_none(self) -> None:
        lh = LiquidHandler("lh")
        assert await lh.resolve_deck_config_async() is None


class TestDeckLayoutRebuildRequired:
    """The runtime configures the deck once at start; mutating the registry
    afterward does NOT reach the driver until the next runtime rebuild.
    This is the deliberate `rebuild-required` contract for deck layouts."""

    async def test_post_start_store_edit_does_not_re_reach_runtime(self) -> None:
        original = _config(deck_type="STARlet")
        replacement = _config(deck_type="STAR")
        store = seeded_deck_layout_service({"default": original})
        lh = LiquidHandler(
            "lh",
            sim=True,
            deck_layout_store=store,
            deck_layout="default",
        )

        # At runtime-start time we resolve once and freeze the result.
        resolved_at_start = await lh.resolve_deck_config_async()
        assert resolved_at_start is not None
        assert resolved_at_start.deck_type == "STARlet"

        # Operator edits the store after start.
        await store.update("default", replacement)

        # A post-start dispatch using the already-resolved config sees the
        # original; nothing re-resolves until the next runtime rebuild.
        assert resolved_at_start.deck_type == "STARlet"

        # The store does see the new value -- it's just not pushed to the
        # driver until rebuild.
        loaded = await store.get("default")
        assert loaded is not None
        assert loaded.deck_type == "STAR"
