"""LabwareGroup and LabwareGroupMember: named bundles of labware for submission.

A LabwareGroup represents one experimental lineage's worth of labware. Members
map to @orca.thread-decorated functions in the workflow by name. The member's
Acquisition variant determines how the engine obtains the physical labware.
"""

from dataclasses import dataclass, field
from typing import Union


class AcquisitionValidationError(ValueError):
    """Raised at submit time when a member's Acquisition cannot be satisfied.

    Example: BarcodeAcquisition whose barcode is not in the ILabwareStore, or
    LocationAcquisition whose source_location does not exist in the topology.
    """


@dataclass(frozen=True)
class PoolAcquisition:
    """Acquire from the template's pool/stacker. Engine picks next available."""


@dataclass(frozen=True)
class BarcodeAcquisition:
    """Acquire labware by barcode via ILabwareStore.

    The engine queries the store at submission accept time. If the barcode is
    not found, the submission is rejected with a named error.
    """
    barcode: str


@dataclass(frozen=True)
class LocationAcquisition:
    """Acquire from a specific on-deck location.

    verify_barcode, if set, is checked against the on-deck scanner reading at
    pickup time; mismatch raises.
    """
    source_location: str
    verify_barcode: str | None = None


Acquisition = Union[PoolAcquisition, BarcodeAcquisition, LocationAcquisition]


@dataclass(frozen=True)
class LabwareGroupMember:
    """One labware participant in a group, keyed by the @orca.thread function name."""
    thread_template_name: str
    acquisition: Acquisition = field(default_factory=PoolAcquisition)


@dataclass(frozen=True)
class LabwareGroup:
    """An experimental lineage's labware bundle.

    Exactly one member per thread_template_name is allowed. Duplicates raise
    at construction.
    """
    id: str
    members: tuple[LabwareGroupMember, ...]
    name: str | None = None

    def __post_init__(self) -> None:
        if ":" in self.id:
            raise ValueError(
                f"LabwareGroup id {self.id!r} must not contain ':'; it delimits "
                "the internal labware slot key."
            )
        seen: set[str] = set()
        for m in self.members:
            if m.thread_template_name in seen:
                raise ValueError(
                    f"LabwareGroup has duplicate member for thread template "
                    f"'{m.thread_template_name}'"
                )
            seen.add(m.thread_template_name)

    def member_for_thread(self, thread_template_name: str) -> LabwareGroupMember | None:
        for m in self.members:
            if m.thread_template_name == thread_template_name:
                return m
        return None
