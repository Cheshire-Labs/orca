"""The one answer to "what scalars does this move get".

Most moves are the same move, so the answer starts from the built-in seed,
picks up whatever this deployment has tuned about the arm, and is narrowed by
whatever is more specific about this one move:

    seed -> arm defaults -> labware -> site -> this labware here -> this one plate

Each step is strictly narrower than the one before it. The labware's profile
comes before the site because it travels with the labware everywhere, where a
nest's clearance is about one place and should win over a general statement
about the type. Last is the one combination narrower than either: this labware,
at this position, which is where a nest that suits every plate but one gets its
exception without changing how that plate is handled anywhere else.

Each narrowing layer contributes a sparse patch and the merge is field-wise, so
a layer sets what it knows and inherits the rest. That is what lets one number
be changed without restating the other nine.

The result travels to the driver as plain scalars. Nothing labware-shaped
crosses, so a transporter driver never needs a labware catalog to work out how
to hold something, and a second driver adapts the same resolved numbers to its
own vocabulary instead of re-deriving them.

`sources` says which layer won each field. Without it a plate that moved oddly
leaves nobody able to say which layer decided that, which is the failure mode a
layered model invites.
"""

from cheshire_drivers.move_parameters import (
    MoveParameterField,
    MoveParameterPatch,
    MoveParameters,
)
from pydantic import BaseModel, ConfigDict, JsonValue, field_serializer, model_validator
from typing_extensions import Self


class TransporterMoveDefaults(BaseModel):
    """One arm's starting numbers, and where each of them came from.

    What the operator surfaces read and return. `sources` maps each field to the
    layer that decided it, so a number this deployment chose does not look like
    one nobody has ever set.
    """

    transporter_name: str
    parameters: MoveParameters
    sources: dict[str, str]


class MoveDefaultsPatchRequest(BaseModel):
    """An edit to one arm's starting numbers, as the surfaces take it.

    `set` names the fields to change and leaves the rest as they are; `clear`
    names fields to hand back to the built-in seed. Both in one body because an
    operator swapping a gripper does both at once, and two requests would leave
    the arm briefly on a mixture neither of them intended.

    Lives here rather than on either REST surface so both surfaces take the
    same body; two copies of a wire shape drift on the first field added.

    Strict, and an edit naming nothing is refused: both are how a body with the
    wrong outer key fails loudly instead of reporting success over an arm whose
    numbers never changed.
    """

    model_config = ConfigDict(extra="forbid")

    set: MoveParameterPatch = MoveParameterPatch()
    clear: list[MoveParameterField] = []

    @model_validator(mode="after")
    def _names_something(self) -> Self:
        if not self.set.model_dump(exclude_none=True) and not self.clear:
            raise ValueError("name at least one field to set, or one to clear")
        return self


class LabwareGripProfile(BaseModel):
    """How one labware type is held, as the operator surfaces read and return it.

    Deliberately not a resolved total record, unlike an arm's defaults. A grip
    profile is a sparse statement that belongs to the type rather than to any
    one arm, so resolving it would mean picking an arm to resolve it against and
    reporting numbers that arm never uses for this labware. What an operator
    needs to see is which fields this type actually claims.
    """

    labware_type: str
    patch: MoveParameterPatch

    @field_serializer("patch")
    def _only_what_the_type_claims(
        self, patch: MoveParameterPatch,
    ) -> dict[str, JsonValue]:
        """Emit the fields this type names and leave the rest out entirely.

        A patch dumped whole puts every untouched field on the wire as null,
        which reads as an opinion the type does not have. Sparseness is the
        whole point of the layer, so the wire says it too.
        """
        return patch.model_dump(exclude_none=True)


class GripProfilePatchRequest(BaseModel):
    """An edit to one labware type's grip profile, as the surfaces take it.

    Same shape as an edit to an arm's defaults: `set` names fields to change,
    `clear` hands fields back to whatever the layers underneath say. Both in one
    body so a type being re-measured never sits on a half-applied mixture.
    """

    set: MoveParameterPatch = MoveParameterPatch()
    clear: list[MoveParameterField] = []
