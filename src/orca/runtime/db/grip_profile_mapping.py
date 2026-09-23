"""DB-neutral mapping between ``MoveParameterPatch`` and ``GripProfileRow``.

Lives in orca-core and is reused by every per-DB grip-profile store. The patch
rides as one JSON blob and comes back through the Pydantic model, so a row
written by an older shape fails validation on read rather than reaching an arm
as a field nobody meant to set.

Only the fields a type actually names are stored: ``exclude_none`` on the way in
keeps "no opinion" out of the row, which is what makes a later edit to the arm's
defaults still reach this labware.
"""

from cheshire_drivers.move_parameters import MoveParameterPatch

from orca.runtime.db.models import GripProfileRow


def grip_profile_to_row(
    labware_type: str, patch: MoveParameterPatch,
) -> GripProfileRow:
    return GripProfileRow(
        labware_type=labware_type,
        patch=patch.model_dump(exclude_none=True),
    )


def row_to_grip_profile(row: GripProfileRow) -> MoveParameterPatch:
    return MoveParameterPatch.model_validate(row.patch)
