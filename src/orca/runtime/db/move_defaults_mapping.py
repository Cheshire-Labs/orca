"""DB-neutral mapping between ``MoveParameterPatch`` and ``MoveDefaultsRow``.

Lives in orca-core and is reused by every per-DB move-defaults store. The blob
comes back through the Pydantic model, so a row holding a field the model no
longer has fails validation on read rather than reaching an arm.
"""

from cheshire_drivers.move_parameters import MoveParameterPatch

from orca.runtime.db.models import MoveDefaultsRow


def move_defaults_to_row(
    transporter_name: str, patch: MoveParameterPatch,
) -> MoveDefaultsRow:
    return MoveDefaultsRow(
        transporter_name=transporter_name,
        patch=patch.model_dump(exclude_none=True),
    )


def row_to_move_defaults(row: MoveDefaultsRow) -> MoveParameterPatch:
    return MoveParameterPatch.model_validate(row.patch)
