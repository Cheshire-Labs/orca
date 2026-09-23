"""The one answer to "what parameters does this pipetting step get".

Most steps are the same step, so the answer starts from what the deployment does
when nobody says, and is narrowed by whatever is more specific about this one:

    defaults  ->  the liquid being moved  ->  the step being run

`PipettingPatch` says what a layer contributing nothing means; this module is
where the layers are named and put in order. The result travels to the driver as
one resolved record, because a driver choosing between layers is how two drivers
end up resolving the same request differently.

`sources` says which layer won each field. Without it a plate pipetted oddly
leaves nobody able to say which layer decided that, which is the failure mode a
layered model invites.
"""

from dataclasses import dataclass
from typing import Sequence

from cheshire_drivers.liquid_handler_models import (
    SEED_PIPETTING_PARAMETERS,
    MixParamsModel,
    PipettingParameters,
    PipettingPatch,
)
from cheshire_drivers.pipetting import PipettingProfile

DEFAULTS = "defaults"
LIQUID_CLASS = "liquid class"
STEP = "step"


@dataclass(frozen=True)
class ResolvedPipettingParameters:
    """What the step got, and which layer each field came from."""

    parameters: PipettingParameters
    sources: dict[str, str]


def profile_patch(profile: PipettingProfile | None) -> PipettingPatch:
    """One authored profile as a patch over whatever is under it.

    `name` is deliberately not carried through: it labels the layer in
    `sources` and is never something a machine acts on.
    """
    if profile is None:
        return PipettingPatch()
    mix = profile.mix
    return PipettingPatch(
        height=profile.height,
        flow_rate=profile.flow_rate,
        blow_out=profile.blow_out,
        blow_out_volume=profile.blow_out_volume,
        blow_out_flow_rate=profile.blow_out_flow_rate,
        mix=(
            None
            if mix is None
            else MixParamsModel(
                volume=mix.volume, repetitions=mix.repetitions, flow_rate=mix.flow_rate
            )
        ),
    )


def _layer_label(profile: PipettingProfile | None, fallback: str) -> str:
    """What `sources` calls this layer: the profile's own name where it has one,
    since "glycerol" tells a reader more than "liquid class"."""
    if profile is not None and profile.name:
        return profile.name
    return fallback


def resolve_pipetting_parameters(
    defaults: PipettingParameters,
    layers: Sequence[tuple[str, PipettingPatch]],
) -> ResolvedPipettingParameters:
    """Fold labelled layers over the defaults, lowest precedence first.

    Every surface that pipettes goes through here. A second fold somewhere else
    is how a plate ends up pipetted one way under a workflow and another way
    when an operator does it by hand.
    """
    parameters = defaults.model_copy(deep=True)
    sources = dict.fromkeys(defaults.model_dump(), DEFAULTS)

    for layer, patch in layers:
        contributed = patch.model_dump(exclude_none=True)
        if not contributed:
            continue
        parameters = patch.apply_to(parameters)
        sources.update(dict.fromkeys(contributed, layer))

    return ResolvedPipettingParameters(parameters=parameters, sources=sources)


def resolve_authored_pipetting(
    liquid_class: PipettingProfile | None = None,
    technique: PipettingProfile | None = None,
) -> ResolvedPipettingParameters:
    """The two profiles an action body passes, resolved and explained."""
    return resolve_pipetting_parameters(
        SEED_PIPETTING_PARAMETERS,
        (
            (_layer_label(liquid_class, LIQUID_CLASS), profile_patch(liquid_class)),
            (_layer_label(technique, STEP), profile_patch(technique)),
        ),
    )


def resolve_pipetting(
    liquid_class: PipettingProfile | None = None,
    technique: PipettingProfile | None = None,
) -> PipettingParameters:
    """The resolved record alone, for a caller that only needs to send it."""
    return resolve_authored_pipetting(liquid_class, technique).parameters


def resolve_wire_pipetting(
    liquid_class: PipettingPatch | None = None,
    technique: PipettingPatch | None = None,
) -> PipettingParameters:
    """The same fold for a surface whose layers arrived as wire patches.

    An operator driving a device ad hoc has to get the answer a workflow gets,
    so REST and MCP resolve here rather than folding the layers themselves.
    """
    return resolve_pipetting_parameters(
        SEED_PIPETTING_PARAMETERS,
        (
            (LIQUID_CLASS, liquid_class or PipettingPatch()),
            (STEP, technique or PipettingPatch()),
        ),
    ).parameters
