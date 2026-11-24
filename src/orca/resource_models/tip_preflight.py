"""The tip check every tip pick-up passes before it is sent, from a run or by hand."""

import logging
from collections.abc import Callable, Mapping, Sequence

from cheshire_drivers.liquid_handler_models import PickUpTips96Request, PickUpTipsRequest
from pydantic import BaseModel

from orca.resource_models.device_error import TipsNotPresentError
from orca.resource_models.labware import LabwareInstance

logger = logging.getLogger("orca")


def tips_claimed_by(request: BaseModel | None, rack_named: Callable[[str], LabwareInstance | None]) -> dict[str, list[str]]:
    """Rack name -> the positions a typed tip pick-up request takes tips from."""
    if isinstance(request, PickUpTipsRequest):
        claimed: dict[str, list[str]] = {}
        for pick in request.picks:
            claimed.setdefault(pick.tip_rack, []).extend(pick.positions)
        return claimed
    if isinstance(request, PickUpTips96Request):
        rack = rack_named(request.tip_rack)
        # A rack the record does not name still claims, so the check logs that it could not run.
        return {request.tip_rack: [] if rack is None else list(rack.every_tip_position())}
    return {}


async def refuse_a_pick_from_empty_spots(
    claimed: Mapping[str, Sequence[str]], rack_named: Callable[[str], LabwareInstance | None],
) -> None:
    """Refuse a tip pick whose positions the record reads empty, before the call reaches the driver.

    Only on a reading nobody has questioned. A rack nobody has looked at since an observation gap (a restart, a
    reconnect, an error pause, a hand reload) logs and lets the pick go: refusing on that reading would stop the first
    pick after every restart on a reading that is not evidence.
    """
    for rack_name, positions in claimed.items():
        rack = rack_named(rack_name)
        if rack is None:
            logger.warning(
                "tip check: positions were claimed on rack '%s', which the record does not name; the check did not "
                "run for it.", rack_name,
            )
            continue
        missing = await rack.missing_tip_positions(list(positions))
        if not missing:
            continue
        if await rack.went_unobserved():
            logger.warning(
                "tip check: rack '%s' reads empty at %s, but nobody has looked at it since the last observation gap; "
                "letting the pick through. Look at the rack, then `orca labware confirm-tips %s` if the record was "
                "right, or `orca labware set-tips %s --position ... --reason ...` if it was reloaded.",
                rack.name, ", ".join(missing), rack.id, rack.id,
            )
            continue
        remaining = await rack.tip_count_present()
        raise TipsNotPresentError(rack.name, rack.id, missing, remaining)
