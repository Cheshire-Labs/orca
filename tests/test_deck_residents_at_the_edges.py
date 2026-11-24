"""Racks and troughs that stay on the deck: what a run does when the record and the deck disagree."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.labware_interfaces import IContainer, ITrough

import orca.resource_models.labware as labware_module
import orca.workflow_models.overflow_strategy as overflow_strategy
from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
from orca.resource_models.device_error import ReagentRanOutError
from orca.resource_models.capacity import CapacityExceededError
from orca.resource_models.labware import TipRackInstance, TroughInstance
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.resource_models.resources import IModeAware
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.workflow_models.actions.device_call_dispatcher import DeviceCallDispatcher
from orca.workflow_models.labware_group import AcquisitionValidationError
from orca.workflow_models.errors import SpawnIncompatibleError
from orca.runtime.system_runtime import SystemRuntime
from orca.state.records import DeviceOperation
from cheshire_drivers.liquid_handler_models import TROUGH_WELL_ID
from orca.workflow_models.acquisition import no_stated_tips
from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.resource_models.labware_directory import LabwareDirectory, LabwarePlacements
from orca.state.placement import PlacementLedger
from tests.test_reuse_bind_identity import (
    RESIDENT_POSITION, _deck_site_start, _executing_workflow, _reuse_template,
)


@pytest.mark.asyncio
async def test_live_asks_a_person_when_nothing_is_at_the_spot() -> None:
    workflow = _executing_workflow(InMemoryLabwareStore())
    start = _deck_site_start()
    resolved = await workflow._resolve_reuse_bind(_reuse_template(start), run_mode=WorkflowRunMode.LIVE)
    assert resolved is None
    assert start.labware is None, "the slot stays empty until a person puts it there"


def _site_of_a_device_in(device_mode: WorkflowRunMode) -> DeckSiteLocation:
    device = MagicMock(spec=IModeAware)
    device.name = "lh"
    device.mode_under = AsyncMock(return_value=device_mode)
    start = DeckSiteLocation(RESIDENT_POSITION, owner=device, site_name="reagent_site")
    start.bind_placements(LabwarePlacements(PlacementLedger(), LabwareDirectory()))
    return start


@pytest.mark.asyncio
@pytest.mark.parametrize("device_mode,a_person_places_it", [
    (WorkflowRunMode.SIM, False),
    (WorkflowRunMode.LIVE, True),
])
async def test_an_empty_site_in_a_live_run_follows_the_mode_of_its_device(
    device_mode: WorkflowRunMode, a_person_places_it: bool,
) -> None:
    """A handler under a sim override in a LIVE run has a simulated deck, so the run places the labware itself."""
    workflow = _executing_workflow(InMemoryLabwareStore())
    start = _site_of_a_device_in(device_mode)
    resolved = await workflow._resolve_reuse_bind(_reuse_template(start), run_mode=WorkflowRunMode.LIVE)
    assert (resolved is None) is a_person_places_it


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [WorkflowRunMode.CHECK_RUN, WorkflowRunMode.SIM])
async def test_a_simulated_run_places_it_itself(mode: WorkflowRunMode) -> None:
    workflow = _executing_workflow(InMemoryLabwareStore())
    start = _deck_site_start()
    resolved = await workflow._resolve_reuse_bind(_reuse_template(start), run_mode=mode)
    assert resolved is not None and start.labware is resolved.labware_instance


def _trough(
    monkeypatch: pytest.MonkeyPatch,
    *,
    volume: float,
    declared: bool = True,
    replenished: bool = False,
    unobserved: bool = False,
    set_by_operator: bool = False,
) -> TroughInstance:
    physical = MagicMock()
    physical.name = "reagent"
    physical.size_z = 10.0
    trough = TroughInstance(physical, template_name="reagent", labware_type="trough")
    trough._template = MagicMock(declared_replenished=replenished, declares_starting_volume=declared)
    op = MagicMock(operation=DeviceOperation.SET_VOLUME, affected_labware=["reagent"])
    trough.ops = AsyncMock(return_value=[op] if set_by_operator else [])
    trough.went_unobserved = AsyncMock(return_value=unobserved)
    monkeypatch.setattr(labware_module, "well_volumes", lambda ops, name: {TROUGH_WELL_ID: volume})
    return trough


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,short", [
    ({"volume": 100.0}, 0.0),
    ({"volume": 50.0}, 0.0),
    ({"volume": 30.0}, 20.0),
    ({"volume": 30.0, "declared": False}, 0.0),
    ({"volume": 30.0, "declared": False, "set_by_operator": True}, 20.0),
    ({"volume": 30.0, "replenished": True}, 0.0),
    ({"volume": 50.0 - 1e-9}, 0.0),
    ({"volume": 30.0, "unobserved": True}, 0.0),
])
async def test_how_short_a_trough_reads(
    kwargs: dict[str, float | bool], short: float, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _trough(monkeypatch, **kwargs).reagent_short_by(50.0, WorkflowRunMode.CHECK_RUN) == short


@pytest.mark.asyncio
async def test_a_replenished_trough_is_still_checked_on_live(monkeypatch: pytest.MonkeyPatch) -> None:
    trough = _trough(monkeypatch, volume=30.0, replenished=True)
    assert await trough.reagent_short_by(50.0, WorkflowRunMode.LIVE) == 20.0


@pytest.mark.asyncio
@pytest.mark.parametrize("device_mode,expected", [
    (WorkflowRunMode.CHECK_RUN, WorkflowRunMode.CHECK_RUN),
    (WorkflowRunMode.LIVE, WorkflowRunMode.LIVE),
])
async def test_the_trough_check_reads_the_mode_of_the_device(
    device_mode: WorkflowRunMode, expected: WorkflowRunMode,
) -> None:
    device = MagicMock(spec=IModeAware)
    device.effective_mode = AsyncMock(return_value=device_mode)
    dispatcher = DeviceCallDispatcher.__new__(DeviceCallDispatcher)
    dispatcher._device = device
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        assert await dispatcher._run_mode_of_this_device() is expected
    finally:
        current_run_mode.reset(token)


def test_the_ran_out_error_says_what_to_do() -> None:
    text = str(ReagentRanOutError("reagent", "id-1", 20.0))
    assert "reagent is 20 uL short" in text and "then RETRY." in text


def test_the_refusal_for_an_unstated_rack_names_the_rack() -> None:
    assert "rack_1" in no_stated_tips("rack_1")


def _pool(name: str) -> MagicMock:
    container = MagicMock(spec=IContainer)
    container.position = None
    container.resource_name = name
    return container


def test_an_aspirate_from_a_trough_claims_the_total_across_channels() -> None:
    interpreter = LiquidHandlerInterpreter()
    claimed = interpreter.claimed_reagent_volumes("aspirate", (_pool("reagent"), [10.0, 10.0, 5.0]), {})
    assert claimed == {"reagent": 25.0}


def test_an_aspirate_from_plate_wells_claims_nothing() -> None:
    well = MagicMock(spec=IContainer)
    well.position = "A1"
    well.resource_name = "plate"
    assert LiquidHandlerInterpreter().claimed_reagent_volumes("aspirate", ([well], [10.0]), {}) == {}


def test_a_96_head_aspirate_from_a_trough_claims_every_channel() -> None:
    trough = MagicMock(spec=ITrough)
    trough.name = "reagent"
    claimed = LiquidHandlerInterpreter().claimed_reagent_volumes("aspirate96", (trough, 10.0), {})
    assert claimed == {"reagent": 960.0}


@pytest.mark.asyncio
async def test_the_wrong_labware_at_the_spot_pauses_and_says_what_to_put_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _executing_workflow(InMemoryLabwareStore())
    template = _reuse_template(_deck_site_start())
    wrong = SpawnIncompatibleError(location="pad", expected_template="wanted", actual_template="other")
    monkeypatch.setattr(workflow, "_resolve_reuse_bind", AsyncMock(side_effect=wrong))
    monkeypatch.setattr(overflow_strategy, "_emit_slot_event", lambda workflow, key, state: None)

    with pytest.raises(CapacityExceededError, match="Discharge it"):
        await workflow._bind_reuse_or_refuse(
            MagicMock(receiver_spent=False, slot_key="slot"), template, WorkflowRunMode.LIVE,
        )


@pytest.mark.asyncio
async def test_a_resident_rack_with_no_stated_tips_is_refused_at_submit() -> None:
    rack = MagicMock(spec=TipRackInstance)
    rack.name = "rack_1"
    runtime = SystemRuntime.__new__(SystemRuntime)
    runtime._system = MagicMock()
    runtime._system.labware_contents.of = AsyncMock(return_value=MagicMock(is_known=False))
    template = MagicMock(start_reuse_existing=True, start_location=MagicMock(labware=rack))
    workflow = MagicMock(auto_spawn_registry={"rack": template})

    with pytest.raises(AcquisitionValidationError, match="rack_1"):
        await runtime._refuse_resident_racks_with_no_stated_tips(workflow, None)
