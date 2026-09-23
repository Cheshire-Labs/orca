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

from typing import Protocol, runtime_checkable
from cheshire_drivers.move_parameters import (
    MoveParameterField,
    MoveParameterPatch,
    MoveParameters,
    SEED_MOVE_PARAMETERS,
)
from cheshire_drivers.teachpoints import Teachpoint
from orca.resource_models.resources import IResource
from collections.abc import Sequence
from dataclasses import dataclass


SEED = "seed"
"""Nobody has said anything about this field; it is the built-in starting point."""
DEFAULTS = "defaults"
"""Somebody tuned this field for this arm, deployment-wide."""
LABWARE = "labware"
"""The labware being carried decided this field, wherever it is going."""
SITE = "site"
"""The position being reached into decided this field."""
SITE_LABWARE = "site_labware"
"""This labware at this one position decided this field."""
CARRY = "carry"
"""Somebody said how to carry THIS piece of labware, for as long as it holds.

The narrowest layer, and the only one about one physical object rather than a
type, a place, or an arm. It exists for what the others cannot say: this plate
came out of the sealer with a lid on it, carry it higher until the lid is off.
"""

LABWARE_OWNED_FIELDS: frozenset[str] = frozenset({"grip_distance_from_top"})
"""How far below its top a labware is gripped, which belongs to the labware.

A handler's own gripper reads it from the labware's grip profile and nowhere
else, so an arm-wide value is never read. It is geometry of the plate rather
than a preference of the arm: one number per arm would be wrong for every
labware but the one it was measured on.
"""

SITE_OWNED_FIELDS: frozenset[str] = frozenset({
    "access_type", "clearance", "z_above", "grasp_offset",
})
"""How a position is entered and left, which belongs to the position, not the arm.

`site_patch` supplies all four for every teachpoint, whether or not an access
config named them, so a deployment-wide value for one of them is overwritten on
every pick and place. They are changed through the access config the teachpoint
references instead.
"""


@dataclass(frozen=True)
class ResolvedMoveParameters:
    """What the move got, and which layer each field came from."""

    parameters: MoveParameters
    sources: dict[str, str]


class MoveParameterEditRefused(ValueError):
    """An edit no layer can store, carrying the fields that make it so.

    Still a ValueError, so callers that already catch one keep working. The
    field list is what stops each surface re-running the rule to work out what
    to tell the caller, which is how the two answers drift apart.
    """

    def __init__(self, message: str, fields: Sequence[str]) -> None:
        super().__init__(message)
        self.fields: list[str] = list(fields)


def contested_fields(
    patch: MoveParameterPatch, clear: Sequence[MoveParameterField] = (),
) -> list[str]:
    """Fields an edit both gives a value and hands back."""
    return sorted(set(patch.model_dump(exclude_none=True)) & set(clear))


def reject_contradiction(
    patch: MoveParameterPatch, clear: Sequence[MoveParameterField] = (),
) -> None:
    """Refuse an edit that both sets and clears a field.

    The two halves ask for opposite things about that field and no stored patch
    can hold both, so whichever way a merge resolves it, half the request is
    dropped and the caller is told the write succeeded.
    """
    contested = contested_fields(patch, clear)
    if contested:
        raise MoveParameterEditRefused(
            f"{', '.join(contested)} named in both set and clear: a field "
            "cannot be given a value and handed back in the same write",
            contested,
        )


def reject_site_owned(
    patch: MoveParameterPatch, clear: Sequence[MoveParameterField] = (),
) -> None:
    """Refuse an edit naming a field the site decides.

    For any layer WIDER than the site: the arm's defaults and a labware's grip
    profile both resolve before the site does, so a value either of them stores
    is overwritten on every move. Accepting one would keep a number no move can
    read and report it back as somebody's choice.

    The layers narrower than the site are a different matter and are not checked
    here: a teachpoint's per-labware override and a single labware's carry
    override both resolve after it, and both are meant to win.
    """
    named = set(patch.model_dump(exclude_none=True)) | set(clear)
    site_owned = sorted(named & SITE_OWNED_FIELDS)
    if site_owned:
        raise MoveParameterEditRefused(
            f"{', '.join(site_owned)} describe how a position is entered and left, so "
            "every teachpoint supplies them on every move and a value stored wider "
            "than the position is never read. Change them on the access config the "
            "teachpoint references, or, for one labware at one position, with a "
            "per-labware override on the teachpoint.",
            site_owned,
        )


def reject_labware_owned(
    patch: MoveParameterPatch, clear: Sequence[MoveParameterField] = (),
) -> None:
    """Refuse an edit naming a field only a labware can answer for.

    For the arm-wide layer. The one reader takes it off the labware's grip
    profile, so a value stored on an arm is never read, and reporting it back
    as somebody's choice is worse than refusing it.
    """
    named = set(patch.model_dump(exclude_none=True)) | set(clear)
    labware_owned = sorted(named & LABWARE_OWNED_FIELDS)
    if labware_owned:
        raise MoveParameterEditRefused(
            f"{', '.join(labware_owned)} describes the labware, not the arm: a "
            "handler's gripper reads it off the labware's grip profile and "
            "nowhere else, so a value stored on an arm is never read. Set it "
            "with `grip-profiles set` on the labware type instead.",
            labware_owned,
        )


def site_patch(teachpoint: Teachpoint) -> MoveParameterPatch:
    """How this position is entered and left, as a patch over the defaults.

    A position that names no approach is reached from above. That is not a guess
    invented here: it is the fallback the rest of the stack already declares (the
    built-in ``default_vertical`` config is auto-created for exactly this, and the
    teachpoint table documents NULL as meaning it), and this is the first place
    that actually applies it rather than assuming somebody else did.

    A position that names an approach the arm has no motion for is refused. That
    one cannot be defaulted: entering a hotel slot from above drives the arm into
    the shelf, so a misspelt approach has to stop here rather than resolve to
    something plausible.

    Raises:
        ValueError: the teachpoint names an approach the arm cannot make.
    """
    approach = (teachpoint.access_type or "vertical").lower()
    if approach == "vertical":
        return MoveParameterPatch(
            access_type="vertical",
            clearance=teachpoint.vertical_clearance,
            z_above=0.0,
            grasp_offset=teachpoint.gripper_offset,
        )
    if approach == "horizontal":
        # Backing out of a slot is the horizontal part; the lift after it is the vertical.
        return MoveParameterPatch(
            access_type="horizontal",
            clearance=teachpoint.horizontal_clearance,
            z_above=teachpoint.vertical_clearance,
            grasp_offset=teachpoint.gripper_offset,
        )
    raise ValueError(
        f"Teachpoint {teachpoint.position_id!r} is reached {teachpoint.access_type!r}, "
        "which is neither 'vertical' nor 'horizontal'."
    )


def _resolve(
    layers: Sequence[tuple[str, MoveParameterPatch]],
) -> ResolvedMoveParameters:
    """Merge sparse layers onto the seed, recording which one won each field."""
    parameters = SEED_MOVE_PARAMETERS
    sources = dict.fromkeys(SEED_MOVE_PARAMETERS.model_dump(), SEED)

    for layer, patch in layers:
        contributed = patch.model_dump(exclude_none=True)
        if not contributed:
            continue
        parameters = patch.apply_to(parameters)
        sources.update(dict.fromkeys(contributed, layer))

    return ResolvedMoveParameters(parameters=parameters, sources=sources)


def resolve_move_defaults(defaults: MoveParameterPatch) -> ResolvedMoveParameters:
    """What an arm's moves start from, before any one site narrows them.

    The operator surfaces read this. It reports a field nobody tuned as coming
    from the seed, which is the difference between "this deployment chose 25"
    and "nobody has ever looked at this number".
    """
    return _resolve(((DEFAULTS, defaults),))


def resolve_move_parameters(
    defaults: MoveParameterPatch,
    teachpoint: Teachpoint,
    labware_patch: MoveParameterPatch | None = None,
    labware_type: str | None = None,
    carry_patch: MoveParameterPatch | None = None,
) -> ResolvedMoveParameters:
    """The scalars for one move, and the layer each of them came from.

    ``labware_patch`` is what the labware being carried says about how it is
    held. None means this move carries nothing, or the type has no profile;
    either way the layer contributes nothing and the answer is unchanged.

    ``labware_type`` additionally selects this position's override for that one
    labware, if it has one. Note what does NOT happen here: a position records
    what it was taught with, and nothing derives a grip height from the
    difference between that labware and the one being moved. Whether a taller
    labware should be gripped higher depends on where the jaws close on it,
    which no catalog number answers, so the offset is stated by whoever measured
    it rather than guessed from a size.

    ``carry_patch`` is how this ONE piece of labware is being carried right now.
    Every layer above is a statement about a type, a place, or an arm, and none
    of them can say that this plate in particular is lidded today.
    """
    at_this_site = teachpoint.by_labware.get(labware_type or "", MoveParameterPatch())
    return _resolve((
        (DEFAULTS, defaults),
        (LABWARE, labware_patch or MoveParameterPatch()),
        (SITE, site_patch(teachpoint)),
        (SITE_LABWARE, at_this_site),
        (CARRY, carry_patch or MoveParameterPatch()),
    ))


@runtime_checkable
class _ResolvesItsOwnMoves(Protocol):
    """A mover that knows the layers narrowing its own moves."""

    async def resolve_handling(
        self, teachpoint: Teachpoint, labware_type: str | None = None,
    ) -> ResolvedMoveParameters: ...


@runtime_checkable
class _HoldsItsMoveDefaults(Protocol):
    """A mover that knows what its deployment has tuned for it."""

    async def move_defaults(self) -> MoveParameterPatch: ...


class _ResourceLookup(Protocol):
    """The slice of the system this needs: name in, resource out."""

    def has_resource(self, name: str) -> bool: ...

    def get_resource(self, name: str) -> IResource: ...


async def resolve_handling_for_device(
    system: _ResourceLookup | None,
    device_name: str,
    teachpoint: Teachpoint,
    labware_type: str | None = None,
) -> ResolvedMoveParameters:
    """`Transporter.resolve_handling` for a caller holding only a device name.

    The operator surfaces reach a transporter by name, and they have to get the
    same answer an execution gets. Two ways of deciding how a labware is handled
    is how a plate moves one way under a workflow and another way when someone
    picks it by hand.

    A name that resolves to nothing (an unmounted topology, an ad-hoc coordinate
    pick at a device the system does not model) still gets a real answer: the
    seed narrowed by the site. It gets no labware layer, because the profiles
    live on the runtime that the name did not resolve against.
    """
    if system is not None and system.has_resource(device_name):
        resource = system.get_resource(device_name)
        if isinstance(resource, _ResolvesItsOwnMoves):
            return await resource.resolve_handling(teachpoint, labware_type)
    return resolve_move_parameters(MoveParameterPatch(), teachpoint)


async def move_defaults_for_device(
    system: _ResourceLookup | None,
    device_name: str,
) -> MoveParameters:
    """A transporter's numbers with nothing narrowing them, for a caller with no site.

    A bare gripper command names no teachpoint, so there is nothing for the site
    layer to say, but it still has to open by the number a pick opens by. Resolving
    the deployment's edit over the seed here is what keeps the button and the pick
    on one answer.

    A name that resolves to nothing gets the seed, the same as the layered read.
    """
    edit = MoveParameterPatch()
    if system is not None and system.has_resource(device_name):
        resource = system.get_resource(device_name)
        if isinstance(resource, _HoldsItsMoveDefaults):
            edit = await resource.move_defaults()
    return resolve_move_defaults(edit).parameters
