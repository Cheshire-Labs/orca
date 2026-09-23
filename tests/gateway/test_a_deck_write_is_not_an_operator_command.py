"""Writing a driver's deck model is the engine's job, not an operator command.

The bench report this closes: the operator console's deck editor sent `add_deck_labware`
straight at a Flex. A trough appeared on the driver's deck, `list-labware`
reported nothing there, and no thread could route to it. `add_deck_labware` is
not `@external`, so no catalog advertises it -- but `@external` governs
discoverability, never invokability, so the hardcoded call went through.

These commands edit a driver's own model of its deck and nothing else. The
engine sends them from the placement chokepoint, which writes the ledger in the
same breath. Sent from an operator surface they move one side of the system.
The operator verbs (`register-labware`, `discharge-labware`) write both.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orca.gateway import adhoc
from orca.gateway.adhoc import ENGINE_ONLY_WORLD_COMMANDS, EngineOnlyCommandError
from orca.gateway.registry.snapshot import DeviceSnapshot
from tests.gateway.mode_doubles import unseeded_await


def _snapshot(device_id: str = "flex_1") -> DeviceSnapshot:
    return DeviceSnapshot(
        type="liquid_handler",
        name=device_id,
        interfaces=[],
        capabilities=[],
        provides_state=False,
        methods={},
        site="test",
        lab="test",
        workcell=None,
        status="ready",
        last_seen=datetime.utcnow(),
    )


def _runtime_declaring_nothing() -> MagicMock:
    runtime = MagicMock()
    runtime.system.has_resource.return_value = False
    runtime.device_registry.get = AsyncMock(return_value=None)
    runtime.topology.list_devices.return_value = []
    return runtime


# Spelled out rather than read off the production dict. Parametrizing over the
# dict means deleting a command deletes its own test, which is the exact defect
# this suite exists to catch.
PROTECTED = {
    "add_deck_labware",
    "remove_deck_labware",
    "reset_deck_labware",
    "reconcile_deck_occupancy",
    "seed_position",
    "ensure_seeded",
    "unseed_position",
    "reset_world",
}


def test_every_world_model_write_is_protected() -> None:
    """The set itself, so dropping one fails here instead of going quiet.

    `reconcile_deck_occupancy` is the one to keep an eye on: it sets deck
    occupancy to exactly what the caller names and drops everything else.
    """
    assert set(ENGINE_ONLY_WORLD_COMMANDS) == PROTECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("command", sorted(PROTECTED))
@patch("orca.gateway.adhoc.device_controller")
@patch("orca.gateway.adhoc.device_connection_tracker")
async def test_a_deck_write_never_reaches_the_device(
    mock_registry: MagicMock, mock_controller: MagicMock, command: str,
) -> None:
    mock_registry.get_device = AsyncMock(return_value=_snapshot())
    mock_controller.execute_command = AsyncMock()

    with pytest.raises(EngineOnlyCommandError) as exc:
        await unseeded_await(adhoc.execute_adhoc_command(
            _runtime_declaring_nothing(), "flex_1", command,
            {"name": "trough", "catalog_ref": "nest_1_reservoir_195ml",
             "at": "D1-slot"},
        ))

    mock_controller.execute_command.assert_not_called()
    assert ENGINE_ONLY_WORLD_COMMANDS[command] in str(exc.value)


@pytest.mark.asyncio
@patch("orca.gateway.adhoc.device_controller")
@patch("orca.gateway.adhoc.device_connection_tracker")
async def test_an_ordinary_command_still_dispatches(
    mock_registry: MagicMock, mock_controller: MagicMock,
) -> None:
    """The negative control: the refusal is a named list, not a mood."""
    mock_registry.get_device = AsyncMock(return_value=_snapshot())
    mock_controller.execute_command = AsyncMock(return_value=None)

    await unseeded_await(adhoc.execute_adhoc_command(
        _runtime_declaring_nothing(), "flex_1", "get_deck_state",
    ))

    mock_controller.execute_command.assert_called_once()
