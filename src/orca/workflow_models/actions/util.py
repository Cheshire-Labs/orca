import asyncio
import logging
from orca.resource_models.devices import Device
from orca.resource_models.labware import AnyLabwareTemplate, LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool
from orca.config import ReservationConfig
from orca.system.reservation_manager.errors import (
    AcquisitionYieldRequested,
    ActionReservationTimeoutError,
    UnresolvableDeadlockContext,
    UnresolvableDeadlockError,
)
from orca.system.reservation_manager.interfaces import IReservationCollection, IThreadReservationCoordinator
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.system_map import IResourceLocator, SystemMap


from typing import Dict, List, Protocol, Set, Union
orca_logger = logging.getLogger("orca")


class DoubleAssignmentError(RuntimeError):
    """Raised when a labware slot already bound to one instance is re-assigned
    to a different instance. Surfaces silent-overwrite corruption (F27) as a
    typed error instead of clobbering. Idempotent same-instance re-assignment
    is allowed (it is harmless)."""


class IActionReservationStatusSink(Protocol):
    """Pluggable status surface for the action-reservation retry loop.

    Lets ``ResourcePoolResolver.resolve_action_location`` tell the
    owning thread when it parks waiting for a reservation and what
    locations it is waiting on, without the resolver knowing about
    ``ExecutingLabwareThread``. The thread implements this protocol so
    the dashboard / snapshot path can emit ``AWAITING_ACTION_RESERVATION``
    with a populated ``waiting_for`` field instead of leaving the thread
    silently parked in ``RESOLVING_ACTION_LOCATION``.
    """

    def notify_awaiting_reservation(self, candidate_position_ids: List[str]) -> None:
        """Called once per retry cycle after a reject/deadlock outcome.

        The list is the candidate locations the resolver is racing
        against; the snapshot's ``waiting_for`` derives from it.
        """
        ...

    def clear_awaiting_reservation(self) -> None:
        """Called on grant or before raising; resets the snapshot field."""
        ...

class LocationCollectionReservationRequest(IReservationCollection):
    def __init__(self, thread_id: str, locations: List[LocationReservation], system_map: SystemMap, reference_point: Location) -> None:
        self._thread_id = thread_id
        self._action_location_requests = locations
        self._reserved_action_location: LocationReservation | None = None
        self._system_map: SystemMap = system_map
        self._reference_point: Location = reference_point
        self._processed = asyncio.Event()
        self._granted = asyncio.Event()
        self._rejected = asyncio.Event()
        self._deadlocked = asyncio.Event()
        self._unresolvable_deadlock = asyncio.Event()
        self._unresolvable_deadlock_context: UnresolvableDeadlockContext | None = None

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def processed(self) -> asyncio.Event:
        return self._processed

    @property
    def granted(self) -> asyncio.Event:
        return self._granted

    @property
    def rejected(self) -> asyncio.Event:
        return self._rejected

    @property
    def deadlocked(self) -> asyncio.Event:
        return self._deadlocked

    @property
    def unresolvable_deadlock(self) -> asyncio.Event:
        return self._unresolvable_deadlock

    @property
    def unresolvable_deadlock_context(self) -> UnresolvableDeadlockContext | None:
        return self._unresolvable_deadlock_context

    def set_unresolvable_deadlock(self, context: UnresolvableDeadlockContext) -> None:
        self._unresolvable_deadlock_context = context
        self._unresolvable_deadlock.set()

    @property
    def reserved_action_location(self) -> LocationReservation:
        if self._reserved_action_location is None:
            raise ValueError("No action location reserved")
        return self._reserved_action_location

    def resolve_final_reservation(self) -> None:
        sorted_requests = self._sort_requests(self._reference_point, self._system_map)
        granted_reservations = [
            r for r in sorted_requests if r.granted.is_set() and not r.is_displaced
        ]
        if len(granted_reservations) == 0:
            self._rejected.set()
            self._processed.set()
            return

        # choose the first granted reservation as the final reservation
        self._reserved_action_location = granted_reservations[0] if granted_reservations else None

        # release all other reservations
        for reservation in granted_reservations[1:]:
            reservation.release_reservation()

        # set the granted event
        self._granted.set()
        self._processed.set()


    def _sort_requests(self, reference_point: Location, system_map: SystemMap) -> List[LocationReservation]:
        return sorted(self._action_location_requests,
                      key=lambda x: system_map.get_distance(reference_point.position_id, x.requested_location.position_id))
    
    def get_reservations(self) -> List:
        return self._action_location_requests
    
    def clear(self) -> None:
        """Reset processed/rejected/deadlocked for retry of a non-granted collection.

        Invariant: a granted LocationCollectionReservationRequest owns a
        real action-location reservation that the owning thread will
        execute against. Clearing it would orphan the underlying location
        lock. ``ResourcePoolResolver.resolve_action_location`` only calls
        clear() on deadlocked or rejected paths, so this guard is
        defensive: a granted collection here means a caller violated the
        retry protocol.
        """
        if self.granted.is_set():
            raise RuntimeError(
                "LocationCollectionReservationRequest.clear() invariant "
                "violated: cannot clear a granted collection; release the "
                "reserved action location's underlying lock first.",
            )
        for action in self._action_location_requests:
            action.clear()
        self._processed.clear()
        self._rejected.clear()
        self._deadlocked.clear()
        self._unresolvable_deadlock.clear()
        self._unresolvable_deadlock_context = None

    def __str__(self) -> str:
        output =  f"Location Action Reservation: Resource Pool: {[r.requested_location.position_id for r in self._action_location_requests]}"
        if self._reserved_action_location:
            output += f" - Reserved Location: {self._reserved_action_location.reserved_location.position_id}"
        else:
            output += " - Not yet reserved"
        return output


class ResourcePoolResolver:
    def __init__(self,
                resource_pool: ResourcePool,
                reservation_config: ReservationConfig | None = None) -> None:
        self._resource_pool = resource_pool
        self._reservation_config = reservation_config or ReservationConfig()

    async def resolve_action_location(self,
                            thread_id: str,
                             reference_point: Location,
                             thread_reservation_manager: IThreadReservationCoordinator,
                             system_map: SystemMap,
                             requesting_labware: LabwareInstance | None = None,
                             status_sink: IActionReservationStatusSink | None = None) -> LocationReservation:
        """Resolve which location in the action's resource pool to reserve.

        ``requesting_labware`` lets the reservation layer tell own-labware
        from cross-thread occupancy (Round 5 S1-B). Each candidate
        ``LocationReservation`` carries the labware reference so
        ``LocationReservationManager.can_reserve`` can grant the
        own-labware-at-target case (entry thread's first action against
        its own start_location) while still rejecting cross-thread
        occupancy so ``ThreadDeadlockDetector`` sees the cycle signal.
        """
        timeout = self._reservation_config.action_reservation_timeout
        retry_interval = self._reservation_config.retry_interval
        start_time = asyncio.get_event_loop().time()

        try:
            while True:
                potential = self._get_potential_action_locations(system_map, requesting_labware)
                request = LocationCollectionReservationRequest(
                    thread_id,
                    potential,
                    system_map,
                    reference_point,
                )
                release_snapshot = thread_reservation_manager.release_snapshot(
                    [r.requested_location.name for r in potential]
                )
                await thread_reservation_manager.submit_reservation_request(thread_id, request)
                await request.processed.wait()

                # S3 Round 1: detector declared an unresolvable deadlock
                # (e.g., blocker thread is `immovable=True`). Raise the typed
                # error to break out of the retry loop -- no amount of waiting
                # will free the blocker.
                if request.unresolvable_deadlock.is_set():
                    context = request.unresolvable_deadlock_context
                    if context is None:
                        # Defensive: set_unresolvable_deadlock should always
                        # attach a context, but guard against a misuse.
                        raise ValueError(
                            "unresolvable_deadlock event set without context"
                        )
                    raise UnresolvableDeadlockError(context)

                if request.granted.is_set():
                    return request.reserved_action_location

                if request.deadlocked.is_set():
                    # The flagged acquirer may be the physical blocker (rule 7:
                    # its plate defers a drain); only the thread can park it.
                    orca_logger.info(
                        "Reservation request collection is deadlocked; "
                        "signalling acquisition yield"
                    )
                    raise AcquisitionYieldRequested()
                if not request.rejected.is_set():
                    raise ValueError("Reservation request collection was not granted")
                orca_logger.info("Reservation request collection was rejected, retrying")

                if timeout is not None:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed >= timeout:
                        raise ActionReservationTimeoutError(
                            thread_id=thread_id,
                            timeout_seconds=timeout,
                            candidate_locations=[
                                r.requested_location.name for r in potential
                            ],
                            last_outcome="rejected",
                        )

                if status_sink is not None:
                    status_sink.notify_awaiting_reservation(
                        [r.requested_location.name for r in potential],
                    )

                request.clear()
                await thread_reservation_manager.wait_for_location_release(
                    release_snapshot, retry_interval
                )
        finally:
            if status_sink is not None:
                status_sink.clear_awaiting_reservation()

    def _get_potential_action_locations(
        self,
        resource_locator: IResourceLocator,
        requesting_labware: LabwareInstance | None = None,
    ) -> List[LocationReservation]:
        potential_locations: Set[Location] = set()
        for resource in self._resource_pool.resources:
            if isinstance(resource, Device) and resource.locations:
                for loc in resource.locations:
                    potential_locations.add(loc)
            else:
                potential_location = resource_locator.get_resource_location(resource.name)
                potential_locations.add(potential_location)

        location_requests: List[LocationReservation] = []
        for location in potential_locations:
            location_request = LocationReservation(location, requesting_labware)
            location_requests.append(location_request)
        return location_requests


class AssignedLabwareManager:
    def __init__(self,
                 expected_input_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 expected_output_templates: List[Union[LabwareTemplate, AnyLabwareTemplate]]) -> None:
        self._expected_input_templates = expected_input_templates
        self._expected_inputs: Dict[LabwareTemplate | AnyLabwareTemplate, LabwareInstance | None] = {template: None for template in expected_input_templates}
        self._expected_output_templates = expected_output_templates
        self._expected_outputs: Dict[LabwareTemplate | AnyLabwareTemplate, LabwareInstance | None] = {template: None for template in expected_output_templates}
        self._frozen = False

    def freeze(self) -> None:
        """Mark assignment final. Called by ``UnresolvedLocationAction.assign()``
        at resolution. After freeze, re-binding a slot to a DIFFERENT instance
        raises DoubleAssignmentError (F27). Before freeze, slot overwrites are
        legitimate."""
        self._frozen = True

    @property
    def expected_input_templates(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._expected_input_templates

    @property
    def expected_output_templates(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._expected_output_templates

    @property
    def expected_inputs(self) -> List[LabwareInstance]:
        if any(held is None for held in self._expected_inputs.values()):
            missing_inputs = [key.name for key, held in self._expected_inputs.items() if held is None]
            raise ValueError(f"Not all expected inputs have been assigned.  Missing: {missing_inputs}")
        return [labware for labware in self._expected_inputs.values() if labware is not None]

    @property
    def assigned_inputs(self) -> List[LabwareInstance]:
        """Assigned input instances, the non-raising twin of ``expected_inputs``
        for the presence path (unassigned slots omitted, not an error)."""
        return [labware for labware in self._expected_inputs.values() if labware is not None]

    @property
    def all_inputs_assigned(self) -> bool:
        return all(labware is not None for labware in self._expected_inputs.values())

    @property
    def unassigned_input_slot_names(self) -> List[str]:
        return [key.name for key, labware in self._expected_inputs.items() if labware is None]

    def is_input_assigned(self, template_slot: LabwareTemplate | AnyLabwareTemplate) -> bool:
        return self._expected_inputs.get(template_slot) is not None

    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        if any(output is None for output in self._expected_outputs.values()):
            raise ValueError("Not all expected outputs have been assigned")
        return [labware for labware in self._expected_outputs.values() if labware is not None]

    def assign_input(self, template_slot: LabwareTemplate, input_labware: LabwareInstance):
        if template_slot in self._expected_inputs.keys():
            existing = self._expected_inputs[template_slot]
            if self._frozen and existing is not None and existing is not input_labware:
                raise DoubleAssignmentError(
                    f"input slot '{template_slot.name}' already bound to "
                    f"{existing.name}; refusing to overwrite with {input_labware.name} "
                    "after assignment was frozen"
                )
            self._expected_inputs[template_slot] = input_labware
        elif any(held is None and isinstance(key, AnyLabwareTemplate) for key, held in self._expected_inputs.items()):
            for key in self._expected_inputs.keys():
                if isinstance(key, AnyLabwareTemplate):
                    self._expected_inputs[key] = input_labware
                    break

        else:
            raise ValueError(f"No available slot for input {input_labware}")
        self.assign_output(template_slot, input_labware)

    def assign_output(self, template_slot: LabwareTemplate, output_labware: LabwareInstance):
        if template_slot in self._expected_outputs.keys():
            existing = self._expected_outputs[template_slot]
            if self._frozen and existing is not None and existing is not output_labware:
                raise DoubleAssignmentError(
                    f"output slot '{template_slot.name}' already bound to "
                    f"{existing.name}; refusing to overwrite with {output_labware.name} "
                    "after assignment was frozen"
                )
            self._expected_outputs[template_slot] = output_labware
        elif any(held is None and isinstance(key, AnyLabwareTemplate) for key, held in self._expected_outputs.items()):
            for key in self._expected_outputs.keys():
                if isinstance(key, AnyLabwareTemplate):
                    self._expected_outputs[key] = output_labware
                    break
        else:
            raise ValueError(f"No available slot for output {output_labware}")

    def __str__(self) -> str:
        return f"Input Manager: {self._expected_inputs}"