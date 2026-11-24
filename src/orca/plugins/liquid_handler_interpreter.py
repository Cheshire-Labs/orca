"""Reads a workflow action's liquid-handler call into ledger records.

The call arrives as the arguments a method body wrote, so this binds them to
the bridge's parameter names, builds the same typed request the bridge sends,
and hands it to the one reader an operator's command uses too. Anything a
record says about a command is decided there; what is decided here is only how
a positional call becomes that request.
"""

import logging
from typing import Callable, Mapping, TypeVar

from cheshire_drivers.labware_interfaces import IPlate, ITipRack, ITrough
from cheshire_drivers.liquid_handler_models import Aspirate96Request, AspirateRequest, LabwareStateResponse
from pydantic import BaseModel

from orca.devices import lh_requests
from orca.state.projections import HEAD96_CHANNEL_COUNT
from orca.state.command_operations import (
    READ_PER_CHANNEL,
    action_context,
    operations_for,
    operations_for_channels,
    operations_for_driver_state,
    warn_unread_channel_errors,
)
from orca.state.records import (
    OperationRecord,
)

logger = logging.getLogger("orca.plugins.liquid_handler_interpreter")


# Mirrors the LiquidHandler bridge signatures (sync-guard test pins it) so
# every bridge-legal arg/kwarg split reaches the same request builder.
_VERB_PARAMS: dict[str, tuple[str, ...]] = {
    "aspirate": (
        "containers", "volumes", "flow_rates", "offsets_z", "use_channels",
        "liquid_class", "technique",
    ),
    "dispense": (
        "containers", "volumes", "flow_rates", "offsets_z", "use_channels",
        "liquid_class", "technique",
    ),
    "pick_up_tips": ("tip_spots",),
    "drop_tips": ("tip_spots",),
    "discard_tips": ("use_channels",),
    "mix": ("containers", "params", "use_channels", "liquid_class", "technique"),
    "aspirate96": ("labware", "volume", "flow_rate", "liquid_height"),
    "dispense96": ("labware", "volume", "flow_rate", "liquid_height"),
    "pick_up_tips96": ("tip_rack",),
    "drop_tips96": ("tip_rack",),
    "return_tips96": (),
}

_REQUEST_BUILDERS: Mapping[str, Callable[..., BaseModel]] = {
    "aspirate": lh_requests.aspirate_request,
    "dispense": lh_requests.dispense_request,
    "pick_up_tips": lh_requests.pick_up_tips_request,
    "drop_tips": lh_requests.drop_tips_request,
    "discard_tips": lh_requests.discard_tips_request,
    "mix": lh_requests.mix_request,
    "aspirate96": lh_requests.aspirate96_request,
    "dispense96": lh_requests.dispense96_request,
    "pick_up_tips96": lh_requests.pick_up_tips96_request,
    "drop_tips96": lh_requests.drop_tips96_request,
    "return_tips96": lh_requests.return_tips96_request,
}

# The 96-head API passes ctx.plate(...)/ctx.tip_rack(...) objects, never name
# strings, and these are the parameters that carry them.
_TYPED_LABWARE = (IPlate, ITipRack, ITrough)
_TYPED_LABWARE_PARAMS = frozenset({"labware", "tip_rack"})

_V = TypeVar("_V")


def _bind_verb_call(
    command: str,
    args: tuple[_V, ...],
    kwargs: dict[str, _V],
) -> dict[str, _V]:
    names = _VERB_PARAMS[command]
    if len(args) > len(names):
        raise TypeError(
            f"{command}() takes at most {len(names)} arguments, "
            f"got {len(args)} positional"
        )
    bound: dict[str, _V] = dict(zip(names, args))
    for key, value in kwargs.items():
        if key in bound:
            raise TypeError(
                f"{command}() got multiple values for argument {key!r}"
            )
        bound[key] = value
    _require_typed_labware(bound)
    return bound


def _require_typed_labware(bound: Mapping[str, _V]) -> None:
    """Refuse a name string where the bridge takes a labware object.

    The 96-head verbs read ``.name`` off what they are given, so a string here
    fails deep inside the request builder with an error about an attribute
    rather than about the call.
    """
    for key in _TYPED_LABWARE_PARAMS & set(bound):
        value = bound[key]
        if value is not None and not isinstance(value, _TYPED_LABWARE):
            raise TypeError(
                f"{key} expected typed labware (IPlate/ITipRack/ITrough), "
                f"got {type(value).__name__}"
            )


class LiquidHandlerInterpreter:
    def interpret(
        self,
        command: str,
        args: tuple[_V, ...],
        kwargs: dict[str, _V],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """Every record this call produced, in the order the channels acted."""
        del result
        context = action_context(
            device_name, affected_labware, affected_labware_ids, action_id, thread_id,
        )
        return operations_for(command, self.request_of(command, args, kwargs), repr(args), context)

    def request_of(
        self, command: str, args: tuple[_V, ...], kwargs: dict[str, _V],
    ) -> BaseModel | None:
        builder = _REQUEST_BUILDERS.get(command)
        if builder is None:
            return None
        return builder(**_bind_verb_call(command, args, kwargs))

    def claimed_reagent_volumes(
        self, command: str, args: tuple[_V, ...], kwargs: dict[str, _V],
    ) -> dict[str, float]:
        """Container name -> total volume this call draws from it, parsed before dispatch."""
        if command not in ("aspirate", "aspirate96"):
            return {}
        bound = _bind_verb_call(command, args, kwargs)
        request = _REQUEST_BUILDERS[command](**bound)
        if isinstance(request, AspirateRequest):
            totals: dict[str, float] = {}
            for target in request.aspirations:
                if target.positions is None:
                    totals[target.labware] = totals.get(target.labware, 0.0) + sum(target.volumes)
            return totals
        if isinstance(request, Aspirate96Request) and isinstance(bound.get("labware"), ITrough):
            return {request.labware: request.volume * HEAD96_CHANNEL_COUNT}
        return {}

    def interpret_per_channel_outcomes(
        self,
        command: str,
        args: tuple[_V, ...],
        kwargs: dict[str, _V],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """One record per channel when the driver reported a partial failure.

        On full success this is empty and ``interpret`` writes the consolidated
        record instead. On partial failure every well in the request gets its
        own record, so a later reader can ask which channels actually went.
        """
        if not isinstance(result, LabwareStateResponse):
            return []
        if not result.per_channel_errors:
            return []
        request = self.request_of(command, args, kwargs)
        if not isinstance(request, READ_PER_CHANNEL):
            warn_unread_channel_errors(command, result.per_channel_errors)
            return []
        return operations_for_channels(
            command, request, result.per_channel_errors,
            action_context(
                device_name, affected_labware, affected_labware_ids,
                action_id, thread_id,
            ),
        )

    def interpret_driver_state(
        self,
        command: str,
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """What the driver reported about its own labware when the call returned."""
        del command
        if not isinstance(result, LabwareStateResponse):
            return []
        return operations_for_driver_state(result, action_context(
            device_name, affected_labware, affected_labware_ids, action_id, thread_id,
        ))
