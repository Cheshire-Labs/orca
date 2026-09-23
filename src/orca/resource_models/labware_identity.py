"""Labware identity data models: relationship types and relationship records."""

from dataclasses import dataclass
from enum import Enum

from pydantic import JsonValue


class RelationshipType(str, Enum):
    DERIVED_FROM = "DERIVED_FROM"
    POOLED_INTO = "POOLED_INTO"
    SPLIT_FROM = "SPLIT_FROM"
    PAIRED_WITH = "PAIRED_WITH"
    SUPPLIED_BY = "SUPPLIED_BY"
    CONSUMED_BY = "CONSUMED_BY"
    SEQUENCED_IN = "SEQUENCED_IN"


@dataclass(frozen=True)
class LabwareRelationship:
    source_id: str
    target_id: str
    relationship_type: RelationshipType
    method_name: str | None = None
    execution_id: str | None = None
    timestamp: float | None = None
    metadata: dict[str, JsonValue] | None = None
