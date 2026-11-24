"""A deck layout is stored by one route, PUT /deck-layouts/{device_id}/{name}; configure-deck only pushes a stored one."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.command_table import DeviceCommand
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig, DeckResourceConfig, LabwareStateResponse
from pydantic import BaseModel, JsonValue
from typing_extensions import Self

from orca.gateway.device_command_runner import NamedCommandResult
from orca.gateway.registry import DeviceSnapshot
from orca.operations.deck_layout_models import UpdateDeckLayoutRequest
from orca.operations.deck_layouts import UpdateDeckLayoutOperation
from orca.operations.operator_device import LiquidHandlerConfigureDeckOperation
from orca.operations.operator_device_models import ConfigureDeckRequest
from orca.operations.protocol import OperationError

CARRIER = "PLT_CAR_L5AC_A00"
CARRIER_ONLY = DeckLayoutConfig(deck_type="FlexDeck", resources=[
    DeckResourceConfig(name="carrier", catalog_ref=CARRIER, rail=3),
])
NAMES_A_PLATE = DeckLayoutConfig(deck_type="FlexDeck", resources=[
    DeckResourceConfig(name="plate", catalog_ref="Cos_96_DW_1mL", rail=3),
])


def _registries() -> MagicMock:
    registries = MagicMock()
    registries.labware_catalog.list = AsyncMock(return_value=[MagicMock(labware_type=CARRIER)])
    return registries


def _runtime(stored: DeckLayoutConfig | None) -> MagicMock:
    runtime = MagicMock()
    runtime.deck_layouts.get = AsyncMock(return_value=stored)
    runtime.deck_layouts.add = AsyncMock(return_value=None)
    runtime.deck_layouts.update = AsyncMock(return_value=None)
    return runtime


def _put(config: DeckLayoutConfig) -> UpdateDeckLayoutRequest:
    return UpdateDeckLayoutRequest(device_id="lh", name="main", config=config)


async def test_a_new_name_is_stored() -> None:
    runtime = _runtime(stored=None)

    await UpdateDeckLayoutOperation(runtime=runtime, registries=_registries()).run(_put(CARRIER_ONLY))

    runtime.deck_layouts.add.assert_awaited_once()
    runtime.deck_layouts.update.assert_not_awaited()


async def test_a_stored_name_is_replaced() -> None:
    runtime = _runtime(stored=CARRIER_ONLY)

    await UpdateDeckLayoutOperation(runtime=runtime, registries=_registries()).run(_put(CARRIER_ONLY))

    runtime.deck_layouts.update.assert_awaited_once()
    runtime.deck_layouts.add.assert_not_awaited()


async def test_a_layout_naming_labware_is_refused_before_anything_is_stored() -> None:
    runtime = _runtime(stored=None)

    with pytest.raises(OperationError) as refused:
        await UpdateDeckLayoutOperation(runtime=runtime, registries=_registries()).run(_put(NAMES_A_PLATE))

    assert refused.value.surface_code() == "validation_error"
    runtime.deck_layouts.add.assert_not_awaited()


async def test_a_device_that_is_not_a_liquid_handler_is_not_found() -> None:
    runtime = _runtime(stored=None)
    runtime.deck_layouts.get = AsyncMock(side_effect=KeyError("lh"))

    with pytest.raises(OperationError) as refused:
        await UpdateDeckLayoutOperation(runtime=runtime, registries=_registries()).run(_put(CARRIER_ONLY))

    assert refused.value.surface_code() == "liquid_handler_not_found"


async def test_a_store_refusal_is_a_validation_error() -> None:
    runtime = _runtime(stored=None)
    runtime.deck_layouts.add = AsyncMock(side_effect=ValueError("rail 3 is taken twice"))

    with pytest.raises(OperationError) as refused:
        await UpdateDeckLayoutOperation(runtime=runtime, registries=_registries()).run(_put(CARRIER_ONLY))

    assert refused.value.surface_code() == "validation_error"


class _Runner:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def device(self, device_id: str) -> DeviceSnapshot:
        return DeviceSnapshot(
            type="lh", name=device_id, interfaces=["ILiquidHandler"], capabilities=[], provides_state=False,
            methods={}, site="s", lab="l", workcell=None, status="ready", last_seen=datetime.now(timezone.utc),
        )

    async def run(
        self, device_id: str, command: DeviceCommand, params: BaseModel, *, reason: str | None = None,
    ) -> BaseModel:
        self.sent.append(command.name)
        return LabwareStateResponse(success=True, labware_state={})

    async def run_named(
        self, device_id: str, command: str, params: Mapping[str, JsonValue],
        *, timeout_seconds: float | None = None, confirm: bool = False, reason: str | None = None,
    ) -> NamedCommandResult:
        raise AssertionError("configure-deck sends through its command, not by name")

    @asynccontextmanager
    async def hold(self, device_id: str) -> AsyncIterator[Self]:
        yield self


async def test_configure_deck_pushes_the_stored_layout() -> None:
    runner = _Runner()
    runtime = _runtime(stored=CARRIER_ONLY)

    await LiquidHandlerConfigureDeckOperation(devices=runner, runtime=runtime).run(
        ConfigureDeckRequest(device_id="lh", layout_name="main", reason=None),
    )

    assert runner.sent == ["configure_deck"]
    runtime.deck_layouts.add.assert_not_awaited()
    runtime.deck_layouts.update.assert_not_awaited()


async def test_configure_deck_with_no_stored_layout_says_how_to_store_one() -> None:
    runner = _Runner()

    with pytest.raises(OperationError) as refused:
        await LiquidHandlerConfigureDeckOperation(devices=runner, runtime=_runtime(stored=None)).run(
            ConfigureDeckRequest(device_id="lh", layout_name="main", reason=None),
        )

    assert refused.value.surface_code() == "deck_layout_not_found"
    assert "PUT /api/deck-layouts/lh/main" in refused.value.message
    assert runner.sent == []


def test_configure_deck_takes_no_layout_of_its_own() -> None:
    with pytest.raises(ValueError):
        ConfigureDeckRequest.model_validate({"device_id": "lh", "layout_name": "main", "deck_layout": {}})
