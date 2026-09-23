"""DeckLayoutFacade contract:

- Reads pass through to the per-device deck-layout store.
- Writes pass through; deck-layout edits are rebuild-required from the
  runtime's perspective but the facade itself does not enforce that
  (the running LiquidHandler resolves at start; subsequent mutations
  take effect on the next rebuild).
- Unknown device_id and 'resource is not a liquid handler' both raise
  KeyError so REST/MCP can translate to 404.
"""

from typing import List

import pytest
from cheshire_drivers.liquid_handler_models import (
    DeckLayoutConfig,
    DeckResourceConfig,
)

from orca.devices.devices import LiquidHandler
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resources import IResource
from orca.runtime.deck_layout_service import seeded_deck_layout_service
from orca.runtime.facades.deck_layouts import DeckLayoutFacade


def _layout(deck_type: str = "BRAVO_96") -> DeckLayoutConfig:
    return DeckLayoutConfig(deck_type=deck_type, resources=[])


def _layout_with_resource(
    deck_type: str, resource_name: str,
) -> DeckLayoutConfig:
    return DeckLayoutConfig(
        deck_type=deck_type,
        resources=[
            DeckResourceConfig(name=resource_name, catalog_ref="cat1", rail=1),
        ],
    )


class _FakeSystemWithLiquidHandlers:
    """Stand-in for ISystem exposing only `get_resource` plus the
    ``devices`` iterable used by ``list_all``.
    """

    def __init__(self, resources: dict[str, IResource]) -> None:
        self._by_name = resources

    def get_resource(self, name: str) -> IResource:
        if name not in self._by_name:
            raise KeyError(f"Resource {name} not found")
        return self._by_name[name]

    @property
    def devices(self) -> List[IResource]:
        return list(self._by_name.values())


def _build_lh(
    name: str, layouts: dict[str, DeckLayoutConfig] | None = None,
    deck_layout: str | None = None,
) -> LiquidHandler:
    return LiquidHandler(
        name=name,
        sim=True,
        deck_layout_store=seeded_deck_layout_service(layouts or {}),
        deck_layout=deck_layout,
    )


def _build_facade(
    resources: dict[str, IResource],
) -> DeckLayoutFacade:
    fake_system = _FakeSystemWithLiquidHandlers(resources)
    return DeckLayoutFacade(fake_system)


class TestDeckLayoutFacade:

    async def test_get_returns_none_when_absent(self) -> None:
        facade = _build_facade({"lh1": _build_lh("lh1")})
        assert await facade.get("lh1", "missing") is None

    async def test_get_returns_stored_value(self) -> None:
        seed = {"primary": _layout("BRAVO_96")}
        facade = _build_facade({"lh1": _build_lh("lh1", layouts=seed)})
        result = await facade.get("lh1", "primary")
        assert result is not None
        assert result.deck_type == "BRAVO_96"

    async def test_list_returns_all_for_device(self) -> None:
        seed = {"a": _layout("BRAVO_96"), "b": _layout("MULTIFLEX")}
        facade = _build_facade({"lh1": _build_lh("lh1", layouts=seed)})
        result = await facade.list("lh1")
        names = {name for name, _ in result}
        assert names == {"a", "b"}

    async def test_list_scoped_to_device(self) -> None:
        facade = _build_facade(
            {
                "lh1": _build_lh("lh1", layouts={"a": _layout("BRAVO_96")}),
                "lh2": _build_lh("lh2", layouts={"b": _layout("MULTIFLEX")}),
            },
        )
        lh1_names = {n for n, _ in await facade.list("lh1")}
        lh2_names = {n for n, _ in await facade.list("lh2")}
        assert lh1_names == {"a"}
        assert lh2_names == {"b"}

    async def test_list_all_walks_every_liquid_handler(self) -> None:
        """Cross-LH listing yields (device, name, config) tuples. Empty stores
        contribute zero rows. Non-LH resources on the system are skipped
        (instance-check guard).
        """
        non_lh = PlatePad("pad-not-an-lh")
        facade = _build_facade(
            {
                "lh1": _build_lh("lh1", layouts={"a": _layout("BRAVO_96")}),
                "lh2": _build_lh("lh2"),
                "lh3": _build_lh("lh3", layouts={"c": _layout("MULTIFLEX")}),
                "pad1": non_lh,  # not a LiquidHandler; skipped
            },
        )
        rows = await facade.list_all()
        assert len(rows) == 2
        keys = {(dev, name) for dev, name, _ in rows}
        assert keys == {("lh1", "a"), ("lh3", "c")}

    async def test_add_then_get_roundtrips(self) -> None:
        facade = _build_facade({"lh1": _build_lh("lh1")})
        await facade.add(
            "lh1", "primary", _layout_with_resource("BRAVO_96", "rack"),
            confirm=True,
        )
        result = await facade.get("lh1", "primary")
        assert result is not None
        assert len(result.resources) == 1

    async def test_update_replaces_value(self) -> None:
        seed = {"primary": _layout("BRAVO_96")}
        facade = _build_facade({"lh1": _build_lh("lh1", layouts=seed)})
        await facade.update(
            "lh1", "primary", _layout("MULTIFLEX"),
            confirm=True,
        )
        result = await facade.get("lh1", "primary")
        assert result is not None
        assert result.deck_type == "MULTIFLEX"

    async def test_delete_returns_true_when_removed(self) -> None:
        seed = {"primary": _layout("BRAVO_96")}
        facade = _build_facade({"lh1": _build_lh("lh1", layouts=seed)})
        assert await facade.delete("lh1", "primary", confirm=True) is True
        assert await facade.get("lh1", "primary") is None

    async def test_delete_returns_false_when_absent(self) -> None:
        facade = _build_facade({"lh1": _build_lh("lh1")})
        assert await facade.delete("lh1", "ghost", confirm=True) is False


class TestUnknownDevice:
    """`get_resource` raises KeyError for missing names; the facade
    propagates so REST/MCP can translate to 404."""

    async def test_get_raises_for_unknown_device(self) -> None:
        facade = _build_facade({"lh1": _build_lh("lh1")})
        with pytest.raises(KeyError, match="not found"):
            await facade.get("ghost", "primary")

    async def test_get_raises_for_non_liquid_handler(self) -> None:
        """A resource that exists but is not a LiquidHandler also 404s."""
        non_lh = PlatePad("pad1")
        facade = _build_facade({"pad1": non_lh})
        with pytest.raises(KeyError, match="not a liquid handler"):
            await facade.get("pad1", "primary")

    async def test_add_raises_for_unknown_device(self) -> None:
        facade = _build_facade({"lh1": _build_lh("lh1")})
        with pytest.raises(KeyError, match="not found"):
            await facade.add(
                "ghost", "primary", _layout("BRAVO_96"),
                confirm=True,
            )


class TestStoreErrorPropagation:

    async def test_add_duplicate_propagates_value_error(self) -> None:
        seed = {"primary": _layout("BRAVO_96")}
        facade = _build_facade({"lh1": _build_lh("lh1", layouts=seed)})
        with pytest.raises(ValueError, match="already registered"):
            await facade.add(
                "lh1", "primary", _layout("MULTIFLEX"),
                confirm=True,
            )

    async def test_update_unknown_propagates_keyerror(self) -> None:
        facade = _build_facade({"lh1": _build_lh("lh1")})
        with pytest.raises(KeyError, match="not found"):
            await facade.update(
                "lh1", "ghost", _layout("BRAVO_96"),
                confirm=True,
            )
