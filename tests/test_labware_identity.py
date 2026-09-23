"""Tests for labware identity: barcode, RelationshipType, ILabwareStore."""

import dataclasses

import pytest

from orca.resource_models.labware import LabwareInstance, PlateInstance
from orca.resource_models.labware_identity import LabwareRelationship, RelationshipType
from orca.runtime.labware_store import InMemoryLabwareStore


class TestLabwareInstanceBarcode:

    def test_barcode_defaults_to_none(self) -> None:
        instance = LabwareInstance("plate_1", "96_well")
        assert instance.barcode is None

    def test_barcode_settable_after_construction(self) -> None:
        instance = LabwareInstance("plate_1", "96_well")
        instance.barcode = "BC-002"
        assert instance.barcode == "BC-002"


class TestRelationshipType:

    def test_all_types_accessible(self) -> None:
        assert RelationshipType.DERIVED_FROM == "DERIVED_FROM"
        assert RelationshipType.POOLED_INTO == "POOLED_INTO"
        assert RelationshipType.SPLIT_FROM == "SPLIT_FROM"
        assert RelationshipType.PAIRED_WITH == "PAIRED_WITH"
        assert RelationshipType.SUPPLIED_BY == "SUPPLIED_BY"
        assert RelationshipType.CONSUMED_BY == "CONSUMED_BY"
        assert RelationshipType.SEQUENCED_IN == "SEQUENCED_IN"


class TestLabwareRelationship:

    def test_frozen_dataclass(self) -> None:
        rel = LabwareRelationship(
            source_id="abc",
            target_id="def",
            relationship_type=RelationshipType.DERIVED_FROM,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            rel.source_id = "xyz"  # type: ignore[misc]

    def test_optional_fields_default_to_none(self) -> None:
        rel = LabwareRelationship(
            source_id="abc",
            target_id="def",
            relationship_type=RelationshipType.POOLED_INTO,
        )
        assert rel.method_name is None
        assert rel.execution_id is None
        assert rel.timestamp is None
        assert rel.metadata is None

    def test_serializes_via_asdict(self) -> None:
        rel = LabwareRelationship(
            source_id="abc",
            target_id="def",
            relationship_type=RelationshipType.DERIVED_FROM,
            method_name="transfer",
        )
        d = dataclasses.asdict(rel)
        assert d["source_id"] == "abc"
        assert d["target_id"] == "def"
        assert d["relationship_type"] == "DERIVED_FROM"
        assert d["method_name"] == "transfer"


class TestInMemoryLabwareStore:

    async def test_register_and_get_by_id(self) -> None:
        store = InMemoryLabwareStore()
        instance = LabwareInstance("plate_1", "96_well")
        await store.register(instance)
        result = await store.get_by_id(instance.id)
        assert result is instance

    async def test_get_by_id_missing_returns_none(self) -> None:
        store = InMemoryLabwareStore()
        assert await store.get_by_id("nonexistent") is None

    async def test_get_by_barcode(self) -> None:
        store = InMemoryLabwareStore()
        instance = LabwareInstance("plate_1", "96_well", barcode="BC-001")
        await store.register(instance)
        result = await store.get_by_barcode("BC-001")
        assert result is instance

    async def test_get_by_barcode_missing_returns_none(self) -> None:
        store = InMemoryLabwareStore()
        assert await store.get_by_barcode("nonexistent") is None

    async def test_get_by_barcode_no_barcode_not_indexed(self) -> None:
        store = InMemoryLabwareStore()
        instance = LabwareInstance("plate_1", "96_well")
        await store.register(instance)
        assert await store.get_by_barcode("plate_1") is None

    async def test_update_location(self) -> None:
        store = InMemoryLabwareStore()
        instance = LabwareInstance("plate_1", "96_well")
        await store.register(instance)
        await store.update_location(instance.id, "shaker_1")
        assert store.get_location(instance.id) == "shaker_1"

    async def test_update_location_missing_id_is_noop(self) -> None:
        store = InMemoryLabwareStore()
        await store.update_location("nonexistent", "shaker_1")
        assert store.get_location("nonexistent") is None

    async def test_record_and_get_relationships(self) -> None:
        store = InMemoryLabwareStore()
        rel = LabwareRelationship(
            source_id="plate_1",
            target_id="plate_2",
            relationship_type=RelationshipType.DERIVED_FROM,
        )
        await store.record_relationship(rel)
        results = await store.get_relationships("plate_1")
        assert len(results) == 1
        assert results[0] is rel

    async def test_get_relationships_returns_both_directions(self) -> None:
        store = InMemoryLabwareStore()
        rel = LabwareRelationship(
            source_id="plate_1",
            target_id="plate_2",
            relationship_type=RelationshipType.DERIVED_FROM,
        )
        await store.record_relationship(rel)
        assert len(await store.get_relationships("plate_1")) == 1
        assert len(await store.get_relationships("plate_2")) == 1

    async def test_get_relationships_empty(self) -> None:
        store = InMemoryLabwareStore()
        assert await store.get_relationships("nonexistent") == []


class TestTemplateBackreference:
    """A LabwareInstance carries a reference back to the template it was minted
    from, or None when constructed bare. (Relocated from the deleted
    test_parent_child_locations.py; unrelated to the removed Location tree.)"""

    async def test_plate_instance_has_template_ref(self) -> None:
        from tests.test_helpers import create_test_plate_template
        template = create_test_plate_template("my_plate")
        instance = await template.create_instance()
        assert instance.template is template

    def test_labware_instance_template_is_none_without_template(self) -> None:
        lw = LabwareInstance("bare", "SomePlate")
        assert lw.template is None
