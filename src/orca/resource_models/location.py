from abc import ABC
import asyncio
from enum import Enum
import logging
from typing import List, Optional, Any, Dict

from orca.devices.device_interfaces import IPlateSource
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.labware_directory import LabwarePlacements
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable, IPlateMover
from orca.resource_models.labware import LabwareInstance

# Per-observer dispatch budget on ``Location._fan_out``.
# Bounds the worst-case latency that a single hung observer (e.g. a
# Transporter wire op to an orca-client busy executing a long-running
# command) can impose on labware-discharge / move / dispose paths.
# 30s is comfortably above any expected orca-client round-trip even
# under load; an observer that exceeds it is logged and skipped so
# the labware-state mutation continues. Observer state self-corrects
# on the next event (the Transporter callbacks are documented as
# idempotent in their own docstring).
_FAN_OUT_OBSERVER_TIMEOUT: float = 30.0

class IResourceLocationObserver(ABC):
    def location_notify(self, event: str, location: "Location", resource: ILabwarePlaceable) -> None:
        pass

class LabwareLocationEvent(str, Enum):
    """Labware-state transition fired by ``Location`` to its observers."""
    INITIALIZED = "initialized"
    PREPARED_FOR_PICK = "prepared_for_pick"
    PICKED = "picked"
    PLACED = "placed"


class ILabwareLocationObserver(ABC):
    async def notify_labware_location_change(self, event: LabwareLocationEvent, location: "Location", labware: LabwareInstance) -> None:
        """Hook called by Location on every labware-state transition.

        Async because some observers must dispatch wire ops (e.g. the
        Transporter observer fires `seed_position` / `ensure_seeded` /
        `unseed_position` to orca-client across the WebSocket). Default
        body is a no-op so subclasses only override the events they care
        about.
        """
        pass

class Location(ILabwarePlaceable):
    def __init__(self, position_id: str, resource: Optional[ILabwarePlaceable] = None) -> None:
        self._position_id = position_id
        self._resource: ILabwarePlaceable = resource if resource else PlatePad(position_id)
        self._options: Dict[str, Any] = {}
        self._resource_observers: List[IResourceLocationObserver] = []
        self._labware_observers: List[ILabwareLocationObserver] = []
        self._availability_condition = asyncio.Condition()
        self._spawn_lock = asyncio.Lock()
        self._placements: LabwarePlacements | None = None

    def bind_placements(self, placements: LabwarePlacements) -> None:
        self._placements = placements
        self._resource.bind_placements(placements)

    @property
    def spawn_lock(self) -> asyncio.Lock:
        """Serializes reuse-bind acquire calls at this location.

        Two concurrent submissions hitting the same auto-spawn slot would
        otherwise race past `location.labware is None` and both create
        fresh `LabwareInstance` objects at the same location. The auto-spawn
        callback wraps its bind logic in `async with location.spawn_lock:`
        so binds on the same Location serialize, while binds on different
        Locations stay parallel.
        """
        return self._spawn_lock

    @property
    def name(self) -> str:
        return self._position_id
                              
    @property
    def position_id(self) -> str:
        return self._position_id

    @property
    def site_name(self) -> str | None:
        """The site's name on its owner's model; None for a location that is not a named site."""
        return None

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._resource.labware

    @property
    def owner_mutex_id(self) -> Optional[str]:
        """Owning device's mutex key; None for system locations."""
        return None

    @property
    def accessible_labware(self) -> Optional[LabwareInstance]:
        """The labware a transporter could physically touch here right now.
        Differs from `labware` on staged-load holders: a loaded plate
        occupies the site but sits INSIDE the device, off the approach
        point, until prepare_for_pick stages it back out."""
        return self._resource.accessible_labware

    @property
    def is_plate_source(self) -> bool:
        """True if this Location is backed by an IPlateSource device.

        Plate sources (e.g., stackers) hold many plates physically; the
        output slot being occupied does NOT mean the stage is blocked.
        Pre-check / spawn paths use this to skip "occupied = blocked"
        semantics that only apply to single-slot stages (PlatePads).

        A device-backed Location has a LabwareStagingBridge as its `_resource`,
        which answers for the device behind it.
        """
        resource = self._resource
        if isinstance(resource, IPlateSource):
            return True
        return isinstance(resource, ILabwarePlaceable) and resource.device_is_plate_source

    @property
    def loaded_labware(self) -> List[LabwareInstance]:
        # A bridge answers `loaded_labware` with what is clamped inside it,
        # while a plate staged at its approach point is still at this slot.
        result = list(self._resource.loaded_labware)
        singular = self._resource.labware
        if singular is not None and singular not in result:
            result.append(singular)
        return result

    def initialize_labware(self, labware: LabwareInstance) -> None:
        """Set the resource's labware state synchronously.

        Conforms to the sync `ILabwarePlaceable.initialize_labware` contract.
        Observer fan-out (which is async because some observers fire wire ops)
        lives in :meth:`notify_initialized`; callers should invoke both:

        ```python
        location.initialize_labware(labware)
        await location.notify_initialized(labware)
        ```

        Splitting these preserves LSP against the sync resource interface
        without forcing the resource layer (PlatePad, DeckSite, etc.) into
        async-no-op signatures.
        """
        self._resource.initialize_labware(labware)

    async def notify_initialized(self, labware: LabwareInstance) -> None:
        """Fire the "initialized" observer event.

        Pair to the sync :meth:`initialize_labware`. Delegates to the shared
        :meth:`_fan_out` helper so initialize-side dispatch parallelizes
        identically to pick / place / dispose dispatch.
        """
        await self._fan_out(LabwareLocationEvent.INITIALIZED, labware)

    async def place_labware(self, labware: LabwareInstance) -> None:
        """State-only placement (no physical move): delegate to the resource's
        flavor-specific ``initialize_labware`` holder write, then fire the
        INITIALIZED observer. The chokepoint calls this for every asserted
        placement (operator place, thread-start-on-deck, reuse-bind,
        boot-rehydrate). Raises ``SlotOccupiedError`` if the slot already holds a
        different instance, so the chokepoint fail-fasts before writing any
        other holder."""
        self._resource.initialize_labware(labware)
        await self.notify_initialized(labware)

    @property
    def resource(self) -> ILabwarePlaceable:
        return self._resource

    @resource.setter
    def resource(self, resource: ILabwarePlaceable) -> None:
        self._resource = resource
        if self._placements is not None:
            resource.bind_placements(self._placements)
        for observer in self._resource_observers:
            observer.location_notify("resource_set", self, resource)

    @property
    def supports_deadlock_resolution(self) -> bool:
        """Delegate to the underlying resource."""
        return self._resource.supports_deadlock_resolution
    
    def set_options(self, options: Dict[str, Any]) -> None:
        self._options = options

    async def _fan_out(self, event: LabwareLocationEvent, labware: LabwareInstance | None) -> None:
        """Fan out a labware-location event to every registered observer in parallel.

        Observers may dispatch wire ops (e.g. the Transporter observer fires
        `seed_position` / `ensure_seeded` / `unseed_position` to orca-client
        across the WebSocket); a serial await chain across N peer-transporter
        observers would multiply per-move latency by N. The base contract on
        ``ILabwareLocationObserver.notify_labware_location_change`` does not
        promise call ordering, so ``asyncio.gather`` is safe.

        Each observer call is wrapped by ``_FAN_OUT_OBSERVER_TIMEOUT`` so a
        single hung observer cannot wedge the dispose / move paths. Timing out
        the offender lets the labware-state mutation complete, and the observer
        reconverges on the next event.
        """
        # A reach with no plate on record moves no labware an observer could follow.
        if labware is None or not self._labware_observers:
            return
        await asyncio.gather(*(
            self._dispatch_observer_with_timeout(observer, event, labware)
            for observer in self._labware_observers
        ))

    async def _dispatch_observer_with_timeout(
        self,
        observer: ILabwareLocationObserver,
        event: LabwareLocationEvent,
        labware: LabwareInstance,
    ) -> None:
        try:
            await asyncio.wait_for(
                observer.notify_labware_location_change(event, self, labware),
                timeout=_FAN_OUT_OBSERVER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logging.getLogger("orca").warning(
                "Labware-location observer %s timed out after %ss on "
                "event=%s location=%s labware=%s; continuing without "
                "this observer. State will reconverge on the next "
                "event (Transporter callbacks are idempotent).",
                type(observer).__name__,
                _FAN_OUT_OBSERVER_TIMEOUT,
                event,
                self._position_id,
                labware.name if labware is not None else None,
            )

    async def prepare_for_place(self, labware: LabwareInstance | None, mover: IPlateMover) -> None:
        await self._resource.prepare_for_place(labware, mover)

    async def prepare_for_pick(self, labware: LabwareInstance | None, mover: IPlateMover) -> None:
        await self._resource.prepare_for_pick(labware, mover)
        await self._fan_out(LabwareLocationEvent.PREPARED_FOR_PICK, labware)

    async def notify_picked(self, labware: LabwareInstance | None, mover: IPlateMover) -> None:
        await self._resource.notify_picked(labware, mover)
        await self._fan_out(LabwareLocationEvent.PICKED, labware)

        # Notify all threads waiting for this location to become available
        async with self._availability_condition:
            self._availability_condition.notify_all()

    async def notify_placed(self, labware: LabwareInstance | None, mover: IPlateMover) -> None:
        await self._resource.notify_placed(labware, mover)
        await self._fan_out(LabwareLocationEvent.PLACED, labware)

        # Wake waiters only when the resource reads empty post-place (a
        # multi-plate source absorbing the plate); occupied sites wake on pick.
        if self.labware is None:
            async with self._availability_condition:
                self._availability_condition.notify_all()

    async def dispose_labware(self, labware: LabwareInstance) -> None:
        """Delegate labware disposal to the underlying resource and notify
        availability observers so threads waiting for this location wake up.

        Called when a thread reaches its end_location: the labware has
        exited the workflow, so any single-occupant resource should clear
        its stored reference so another thread can use this location.
        """
        await self._resource.dispose_labware(labware)
        await self._fan_out(LabwareLocationEvent.PICKED, labware)
        async with self._availability_condition:
            self._availability_condition.notify_all()

    async def wait_until_available(self, timeout: Optional[float] = None) -> None:
        """
        Wait until this location becomes available (empty).
        Event-driven - instant notification when labware is picked.

        Args:
            timeout: Optional timeout in seconds. If None, waits indefinitely.

        Raises:
            asyncio.TimeoutError: If timeout expires before location becomes available.
        """
        async with self._availability_condition:
            while self.labware is not None:
                if timeout:
                    await asyncio.wait_for(self._availability_condition.wait(), timeout)
                else:
                    await self._availability_condition.wait()

    def __str__(self) -> str:
        return f"Location: {self._position_id}"
    
    def is_observed_by(self, observer: ILabwareLocationObserver) -> bool:
        return observer in self._labware_observers

    def add_observer(self, observer: IResourceLocationObserver | ILabwareLocationObserver) -> None:
        if isinstance(observer, ILabwareLocationObserver):
            if observer in self._labware_observers:
                return
            self._labware_observers.append(observer)
        elif isinstance(observer, IResourceLocationObserver):
            if observer in self._resource_observers:
                return
            self._resource_observers.append(observer)
        else:
            raise NotImplementedError(f"Observer type {type(observer)} not supported")

    def remove_observer(self, observer: IResourceLocationObserver | ILabwareLocationObserver) -> None:
        if isinstance(observer, ILabwareLocationObserver):
            if observer in self._labware_observers:
                self._labware_observers.remove(observer)
        elif isinstance(observer, IResourceLocationObserver):
            if observer in self._resource_observers:
                self._resource_observers.remove(observer)
