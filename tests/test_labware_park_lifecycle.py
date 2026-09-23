"""Tests for labware park lifecycle: state machine, registry, park template, finder."""

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_state import (
    Found,
    InMemoryLabwareRegistry,
    LabwareState,
    NoMatch,
    NoneAvailable,
)
from orca.workflow_models.park_template import ParkTemplate, WakeReason


# --- LabwareState ---

class TestLabwareState:
    def test_state_values(self) -> None:
        assert LabwareState.AVAILABLE == "AVAILABLE"
        assert LabwareState.IN_JOURNEY == "IN_JOURNEY"
        assert LabwareState.PARKED == "PARKED"
        assert LabwareState.ENDED == "ENDED"


# --- ParkTemplate ---

class TestParkTemplate:
    def test_construction(self) -> None:
        p = ParkTemplate("stacker_7")
        assert p.location == "stacker_7"
        assert p.name == "park:stacker_7"

    def test_location_required(self) -> None:
        p = ParkTemplate("bravo_384_pad")
        assert p.location == "bravo_384_pad"


# --- WakeReason ---

class TestWakeReason:
    def test_values(self) -> None:
        assert WakeReason.ASSIGNMENT == "assignment"
        assert WakeReason.SHUTDOWN == "shutdown"
        assert WakeReason.ABORT == "abort"


# --- InMemoryLabwareRegistry ---

def _make_labware(name: str, barcode: str | None = None) -> LabwareInstance:
    return LabwareInstance(name, "Plate", barcode=barcode)


class TestRegistryBasics:
    def test_register_and_get_state(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw = _make_labware("plate_1")
        registry.register(lw, "plate_1", LabwareState.AVAILABLE)
        assert registry.get_state(lw.id) == LabwareState.AVAILABLE

    def test_update_state(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw = _make_labware("plate_1")
        registry.register(lw, "plate_1", LabwareState.AVAILABLE)
        registry.update_state(lw.id, LabwareState.IN_JOURNEY)
        assert registry.get_state(lw.id) == LabwareState.IN_JOURNEY

    def test_update_state_unknown_raises(self) -> None:
        registry = InMemoryLabwareRegistry()
        with pytest.raises(KeyError):
            registry.update_state("nonexistent", LabwareState.PARKED)

    def test_get_state_unknown_returns_none(self) -> None:
        registry = InMemoryLabwareRegistry()
        assert registry.get_state("nonexistent") is None

    def test_all_parked(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw1 = _make_labware("tips_384")
        lw2 = _make_labware("tips_384")
        lw3 = _make_labware("plate_1")
        registry.register(lw1, "tips_384", LabwareState.PARKED)
        registry.register(lw2, "tips_384", LabwareState.IN_JOURNEY)
        registry.register(lw3, "plate_1", LabwareState.PARKED)
        parked = registry.all_parked()
        assert len(parked) == 2
        parked_ids = {p.instance.id for p in parked}
        assert lw1.id in parked_ids
        assert lw3.id in parked_ids

    def test_update_thread(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw = _make_labware("plate_1")
        registry.register(lw, "plate_1", LabwareState.AVAILABLE)
        mock_thread = object()
        registry.update_thread(lw.id, mock_thread)
        entry = registry._entries[lw.id]
        assert entry.thread is mock_thread


class TestFindAndClaim:
    def test_none_available_when_no_parked(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw = _make_labware("tips_384")
        registry.register(lw, "tips_384", LabwareState.IN_JOURNEY)
        result = registry.find_and_claim("tips_384")
        assert isinstance(result, NoneAvailable)

    def test_none_available_when_no_template_match(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw = _make_labware("plate_1")
        registry.register(lw, "plate_1", LabwareState.PARKED)
        result = registry.find_and_claim("tips_384")
        assert isinstance(result, NoneAvailable)

    def test_found_no_constraints(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw = _make_labware("tips_384")
        registry.register(lw, "tips_384", LabwareState.PARKED)
        result = registry.find_and_claim("tips_384")
        assert isinstance(result, Found)
        assert result.registered.instance is lw
        assert result.registered.state == LabwareState.IN_JOURNEY
        assert registry.get_state(lw.id) == LabwareState.IN_JOURNEY

    def test_found_with_barcode_constraint(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw1 = _make_labware("primer", barcode="FWD-123")
        lw2 = _make_labware("primer", barcode="FWD-456")
        registry.register(lw1, "primer", LabwareState.PARKED)
        registry.register(lw2, "primer", LabwareState.PARKED)
        result = registry.find_and_claim("primer", constraints={"barcode": "FWD-456"})
        assert isinstance(result, Found)
        assert result.registered.instance is lw2
        assert registry.get_state(lw1.id) == LabwareState.PARKED
        assert registry.get_state(lw2.id) == LabwareState.IN_JOURNEY

    def test_no_match_when_barcode_wrong(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw = _make_labware("primer", barcode="FWD-123")
        registry.register(lw, "primer", LabwareState.PARKED)
        result = registry.find_and_claim("primer", constraints={"barcode": "FWD-999"})
        assert isinstance(result, NoMatch)
        assert "FWD-999" in result.reason
        assert registry.get_state(lw.id) == LabwareState.PARKED

    def test_fifo_ordering(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw1 = _make_labware("tips_384")
        lw2 = _make_labware("tips_384")
        registry.register(lw1, "tips_384", LabwareState.PARKED)
        registry.register(lw2, "tips_384", LabwareState.PARKED)
        result = registry.find_and_claim("tips_384")
        assert isinstance(result, Found)
        assert result.registered.instance is lw1

    def test_claimed_thread_not_found_again(self) -> None:
        registry = InMemoryLabwareRegistry()
        lw = _make_labware("tips_384")
        registry.register(lw, "tips_384", LabwareState.PARKED)
        result1 = registry.find_and_claim("tips_384")
        assert isinstance(result1, Found)
        result2 = registry.find_and_claim("tips_384")
        assert isinstance(result2, NoneAvailable)


# --- orca.park() ---

class TestOrcaPark:
    def test_park_returns_park_template(self) -> None:
        import orca.orca as orca
        result = orca.park("stacker_7")
        assert isinstance(result, ParkTemplate)
        assert result.location == "stacker_7"


# --- LabwareInstance.metadata ---

class TestLabwareMetadata:
    def test_metadata_empty_by_default(self) -> None:
        lw = LabwareInstance("plate_1", "Plate")
        assert lw.metadata == {}

    def test_metadata_mutable(self) -> None:
        lw = LabwareInstance("plate_1", "Plate")
        lw.metadata["fwd_barcode"] = "FWD-123"
        assert lw.metadata["fwd_barcode"] == "FWD-123"
