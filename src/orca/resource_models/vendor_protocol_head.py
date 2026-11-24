"""A vendor protocol runs only on a head the record knows is empty.

Orca cannot see the tips a vendor protocol takes on or off, so it assumes one starts and ends with an empty head.
Nothing Orca records during the protocol touches the head, so a head known empty before it is still empty after.
"""

from collections.abc import Callable
from typing import Protocol

from orca.gateway.controller.command_pipeline import Check
from orca.resource_models.device_error import TipsOnTheHeadError
from orca.state.mounted import MountedTips
from orca.state.provenance import Provenance

VENDOR_PROTOCOL = "run_protocol"


class IHeadRecord(Protocol):
    async def of(self, device_name: str) -> MountedTips: ...


class VendorProtocolHeadCheck:
    def __init__(self, mounted_tips: IHeadRecord, is_liquid_handler: Callable[[str], bool]) -> None:
        self._mounted_tips = mounted_tips
        self._is_liquid_handler = is_liquid_handler

    def checks_for(self, device_name: str, command: str) -> tuple[Check, ...]:
        if command != VENDOR_PROTOCOL or not self._is_liquid_handler(device_name):
            return ()
        return (lambda: self._refuse_unless_empty(device_name),)

    async def _refuse_unless_empty(self, device_name: str) -> None:
        head = await self._mounted_tips.of(device_name)
        if head.provenance is Provenance.UNKNOWN:
            raise TipsOnTheHeadError(device_name, "nothing has ever stated what the head carries")
        if head.provenance is Provenance.STALE:
            raise TipsOnTheHeadError(device_name, "nobody has looked at the head since it went unwatched")
        if head.by_channel or head.head96:
            raise TipsOnTheHeadError(device_name, "the record says tips are on the head")
