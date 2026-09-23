"""LabwareStagingBridge -- internal orchestration bridge between orca Locations and Devices.

Runs the stage/load lifecycle and delegates hardware hooks to the owning Device,
so subclass overrides (e.g. Venus) are preserved.

Unrelated to a hosted device-integration gateway. "Bridge" here means wiring
labware-placement events between a Location and a Device's hardware hooks.
"""

import logging
from typing import List, Optional

from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable, IPlateMover
from orca.resource_models.position_occupancy import PositionOccupancy
from orca.state.placement import Reach

orca_logger = logging.getLogger("orca")


class LabwareStagingBridge(ILabwarePlaceable):
    """Orchestration bridge between orca Locations and Devices.

    Backs the site Location of a device that declares ``site_names`` (a deck
    liquid handler gets ``DeviceDeckSite`` instead). Site selection on a
    multi-site device is resolved by the thread against the flat site nodes,
    not here.

    Staged and loaded are one record at two reaches, not two stores. Loading is
    a clamp on the same physical position, so a loaded plate keeps the site
    occupied; the old loaded-reads-empty split would stack plates.
    """

    def __init__(self, name: str, device: Device) -> None:
        self._name = name
        self._device = device
        self._occupancy = PositionOccupancy(name)

    @property
    def name(self) -> str:
        return self._name

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._occupancy.labware

    @property
    def accessible_labware(self) -> Optional[LabwareInstance]:
        """Only a staged plate is at the approach point; a loaded one is inside
        the device until prepare_for_pick stages it back out."""
        return self._occupancy.accessible_labware

    @property
    def loaded_labware(self) -> List[LabwareInstance]:
        return self._occupancy.inside_labware

    @property
    def device(self) -> Device:
        return self._device

    @property
    def supports_deadlock_resolution(self) -> bool:
        return False

    def initialize_labware(self, labware: LabwareInstance) -> None:
        # Seeding what is already here must not un-load it: arrive() would
        # reset the reach to AT_HAND and report a clamped plate as staged.
        if self._occupancy.holds(labware):
            return
        self._occupancy.arrive(labware)

    def remove_loaded(self, labware: LabwareInstance) -> None:
        """Drop a resident without running the transit-stage lifecycle
        (operator discharge, edit-location source clear)."""
        self._occupancy.leave(labware)

    async def prepare_for_place(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        occupant = self._occupancy.labware
        if occupant is not None and occupant is not labware:
            self._refuse(occupant)
        orca_logger.info(f"{self} - preparing for place of {labware}")
        # Same lock the action dispatcher holds, so a place can't drive the
        # device concurrently. Order is always transporter.lock then device.lock.
        async with self._device.lock.held_for("prepare_for_place"):
            await self._device._do_prepare_for_place(labware, mover)

    async def prepare_for_pick(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        if self._occupancy.accessible_labware is labware:
            return
        orca_logger.info(f"{self} - preparing for pick of {labware}")
        async with self._device.lock.held_for("prepare_for_pick"):
            await self._device._do_prepare_for_pick(labware, mover)
            self._occupancy.reached(labware, Reach.AT_HAND)

    async def notify_placed(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        staged = self._occupancy.accessible_labware
        if staged is not None and staged is not labware:
            raise ValueError(
                f"{self} - Labware {labware} placed with {staged} already on stage."
            )
        self._occupancy.arrive(labware)
        orca_logger.info(f"{self} - labware {labware} received on stage")
        # Projection op + ledger write share ONE device.lock section, else a
        # concurrent reconcile sees a torn state (move then record).
        async with self._device.lock.held_for("notify_placed"):
            await self._device._do_notify_placed(labware, mover)
            self._occupancy.reached(labware, Reach.INSIDE)

    async def notify_picked(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        # No staged-match guard. The mover records the plate at its gripper
        # the instant the pick returns, and one record cannot hold it at two
        # positions, so by the time this runs the site is legitimately empty.
        # The guard was catching drift between two stores that no longer exist.
        orca_logger.info(f"{self} - labware {labware} picked from stage")
        self._occupancy.leave(labware)
        async with self._device.lock.held_for("notify_picked"):
            await self._device._do_notify_picked(labware, mover)

    def reset_loaded_labware(self) -> None:
        """Drop every reference this site holds. Driven by clear-all, which
        reaches a bridge by a Location walk rather than the holder loop."""
        for occupant in list(self._occupancy.loaded_labware):
            self._occupancy.leave(occupant)

    async def dispose_labware(self, labware: LabwareInstance) -> None:
        # Operator-set and discharge paths route a resident through here, and
        # it may be staged or loaded; one record covers both.
        self._occupancy.leave(labware)

    def _refuse(self, occupant: LabwareInstance) -> None:
        from orca.resource_models.device_error import SlotOccupiedError
        raise SlotOccupiedError(
            position_id=self._name,
            existing_labware_name=occupant.name,
            existing_template_name=occupant.template_name,
        )

    def __str__(self) -> str:
        return f"LabwareStagingBridge: {self._name}"
