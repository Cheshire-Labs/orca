from orca.resource_models.deck_site import DeckSite
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance


class DeviceDeckSite(DeckSite):
    """A flat deck site that invokes its owning device's present/stow hooks
    under the device lock.

    The driver-facing target is the bare site name ('C2-slot'), stripped
    from the node id ('flex/C2-slot').
    """

    def __init__(self, name: str, device: Device) -> None:
        super().__init__(name)
        self._device = device
        self._driver_site = name.split("/", 1)[1] if "/" in name else name

    @property
    def device(self) -> Device:
        return self._device

    @property
    def driver_site(self) -> str:
        """The site label the driver accepts ('C2-slot', 'carrier-7-0')."""
        return self._driver_site

    async def prepare_for_place(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        async with self._device.lock.held_for("prepare_for_place"):
            await self._device._do_prepare_for_place(
                labware, target=self._driver_site, mover=mover,
            )

    async def notify_placed(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        # Driver hook first, both under one lock: if it raises, the slot stays
        # empty so a retry can re-place instead of meeting "target occupied".
        async with self._device.lock.held_for("notify_placed"):
            await self._device._do_notify_placed(
                labware, target=self._driver_site, mover=mover,
            )
            await super().notify_placed(labware, mover)

    async def prepare_for_pick(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        async with self._device.lock.held_for("prepare_for_pick"):
            await self._device._do_prepare_for_pick(
                labware, target=self._driver_site, mover=mover,
            )

    async def notify_picked(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        async with self._device.lock.held_for("notify_picked"):
            await super().notify_picked(labware, mover)
            await self._device._do_notify_picked(
                labware, target=self._driver_site, mover=mover,
            )
