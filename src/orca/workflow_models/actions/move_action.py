from abc import ABC, abstractmethod
import asyncio
import logging
import uuid
from typing import List, Protocol, Sequence
from orca.resource_models.device_error import DeviceUnderExternalControlError
from orca.resource_models.deck_access import clear_the_deck_for
from orca.resource_models.external_control import device_under_external_control
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placement import LabwarePlacer
from orca.resource_models.labware_location_service import ILabwareLocationService
from orca.state.placement import PlacementState
from orca.resource_models.location import Location
from orca.events.execution_context import MoveActionExecutionContext, ThreadExecutionContext
from orca.resource_models.transporter_base import TransporterBase
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
    ReservationPriority,
)
from orca.workflow_models.action_state_machine import ActionEvent, ActionStateMachine
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.status_enums import ActionStatus


orca_logger = logging.getLogger("orca")


class LabwareNotAtSourceError(Exception):
    """The move's source no longer holds the labware the move was planned for.

    A source is fixed when the path is planned and the grant can be hours later,
    so an operator has every chance to move the plate in between. Raised rather
    than picking whatever is there: a stranger at the source would otherwise be
    carried away under our labware's name.

    Reaching this means the ledger was not told. Once it is, the thread plans a
    fresh move from wherever the plate now is, so the refusal is only ever seen
    for a plate whose whereabouts nobody recorded.
    """

    def __init__(
        self, labware_name: str, source_position_id: str,
        occupant_name: str | None, ledger_position_id: str | None,
    ) -> None:
        self.labware_name = labware_name
        self.source_position_id = source_position_id
        self.occupant_name = occupant_name
        self.ledger_position_id = ledger_position_id
        holds = (
            f"holds {occupant_name} instead" if occupant_name is not None
            else "is empty"
        )
        whereabouts = (
            f"the ledger has it at {ledger_position_id}"
            if ledger_position_id is not None
            else "the ledger does not place it anywhere"
        )
        super().__init__(
            f"move of {labware_name} from {source_position_id}: that site "
            f"{holds}, and {whereabouts}. Say where the plate really is with "
            f"labware edit-location and RETRY -- the move is planned again "
            f"from there, wherever there is. CONTINUE instead only if you "
            f"carried it to the target yourself."
        )


class IHoldsTheSourceSlot(Protocol):
    """Takes the slot a move's plate is still standing in, for as long as it is."""

    async def hold_the_source(
        self, thread_id: str, labware: LabwareInstance, source: Location,
    ) -> LocationReservation | None:
        ...


class IMoveAction(ABC):
    @property
    @abstractmethod
    def id(self) -> str:
        pass

    @property
    @abstractmethod
    def labware(self) -> LabwareInstance:
        pass

    @property
    @abstractmethod
    def source(self) -> Location:
        pass

    @property
    @abstractmethod
    def target(self) -> Location:
        pass

    @property
    @abstractmethod
    def transporter(self) -> TransporterBase:
        pass

    @property
    @abstractmethod
    def release_reservation_on_place(self) -> bool:
        pass

    @property
    @abstractmethod
    def reservation(self) -> LocationReservation:
        pass

    @abstractmethod
    def set_reservation(self, reservation: LocationReservation) -> None:
        pass

    @abstractmethod
    def set_release_reservation_on_place(self, release: bool) -> None:
        pass

    @property
    @abstractmethod
    def target_still_to_be_told(self) -> bool:
        """This move set the plate down and the call telling the target so did
        not return. Survives the pause: the retry that owes that call is a
        fresh executable over the same action."""
        pass

    @abstractmethod
    def set_target_still_to_be_told(self, owed: bool) -> None:
        pass

class ExecutableMoveAction(IMoveAction):
    def __init__(self,
                 status_manager: StatusManager,
                 context: ThreadExecutionContext,
                 action: IMoveAction,
                 labware_location_service: ILabwareLocationService,
                 labware_placer: LabwarePlacer,
                 slot_holder: IHoldsTheSourceSlot) -> None:
        super().__init__()
        self._status_manager = status_manager
        self._action = action
        self._context = context
        self._slot_holder = slot_holder
        self._labware_location_service = labware_location_service
        self._labware_placer = labware_placer
        self._state_machine = ActionStateMachine()
        self._publish_status(self._state_machine.current)
        # CREATED is transitional for moves: the wrapper immediately fires
        # MOVE_RESERVATION_AWAITED so AWAITING_MOVE_RESERVATION is the steady
        # state observers see. This mirrors the pre-2A property-setter
        # double-write at construction. Don't "simplify" the two-call init
        # into a single publish — the dual events are part of the wire shape.
        self._fire(ActionEvent.MOVE_RESERVATION_AWAITED)

    @property
    def status(self) -> ActionStatus:
        return self._state_machine.current

    def _fire(self, event: ActionEvent) -> None:
        # Two-step orchestrator: validate the transition, then publish.
        # MUST stay sync (no awaits between the two steps); inserting an
        # await opens a window where state_machine.current and the
        # StatusManager registry would disagree, and the legal-events set
        # at the time of the next ``_fire`` would be derived from a state
        # that hasn't been published yet.
        new_status = self._state_machine.transition(event)
        self._publish_status(new_status)

    def _publish_status(self, status: ActionStatus) -> None:
        id = self._action.id
        context = MoveActionExecutionContext(
                                 execution_id=self._context.execution_id,
                                 workflow_name=self._context.workflow_name,
                                 thread_id=self._context.thread_id,
                                 thread_name=self._context.thread_name,
                                 template_name=self._context.template_name,
                                 action_id=id,
                                 action_status=status.name.upper(),
                             )
        self._status_manager.set_status("ACTION", id, status.name, context)

    def _jaws_hold_our_labware(self) -> bool:
        """True when this move's plate is already in the mover's jaws.

        One place to ask it. Three sites used to write it out separately, and
        one of them drifting to "any plate at all" is what made every move
        queued behind an in-flight one skip its wait.
        """
        return self._action.transporter.labware == self._action.labware

    def _move_is_already_done(self) -> bool:
        """True when the labware is already resting at this move's target.

        Two witnesses, either one enough. The target itself holds the labware,
        which an operator asserting the position writes and so does a place
        whose arrival got as far as the slot. Or the ledger names the target
        and the slot does not, which is a place whose arrival failed before
        the slot write.

        Neither witness says the target was TOLD. When this move set the
        plate down and the call saying so raised, ``target_still_to_be_told``
        carries that debt and the retry pays it without actuating.

        Labware still in the jaws is never done however the rest reads, so the
        gripper is checked first: that move still owes a place.
        """
        if self._jaws_hold_our_labware():
            return False
        if self._action.target.labware is self._action.labware:
            return True
        return self._ledger_position_id() == self._action.target.position_id

    def _require_labware_at_source(self) -> None:
        """Refuse a pick whose source no longer holds this move's labware."""
        occupant = self._action.source.labware
        if occupant is self._action.labware:
            return
        raise LabwareNotAtSourceError(
            labware_name=self._action.labware.name,
            source_position_id=self._action.source.position_id,
            occupant_name=occupant.name if occupant is not None else None,
            ledger_position_id=self._ledger_position_id(present_only=False),
        )

    def _ledger_position_id(self, *, present_only: bool = True) -> str | None:
        """The position the ledger has this labware at, or None.

        By position id, not object identity: the location service itself treats
        two Locations sharing one position id as the same place.

        ``present_only`` is what "is this move already done" needs: only a
        PRESENT placement is a claim that the labware is really there. A
        refusal message wants the position whatever the placement says, because
        naming where the engine last had it is the point.
        """
        try:
            if (
                present_only
                and self._labware_location_service.placement(self._action.labware)
                is not PlacementState.PRESENT
            ):
                return None
            return self._labware_location_service.get(self._action.labware).position_id
        except KeyError:
            return None

    async def _execute_action(self) -> None:
        await self._make_room_at_both_ends()
        async with self._action.transporter.lock.held_for(
            f"move to {self._action.target.name}"
        ):
            if self._move_is_already_done():
                if self._action.target_still_to_be_told:
                    await self._finish_telling_the_target()
            else:
                source_hold = await self._hold_the_slot_the_plate_is_still_in()
                try:
                    await self._actuate()
                finally:
                    if source_hold is not None:
                        source_hold.release_reservation()

        if self._action.release_reservation_on_place:
            self._action.reservation.release_reservation()

    async def _finish_telling_the_target(self) -> None:
        """Re-make the arrival call this move already owes the target.

        The arm let go and then the call saying so raised. Nothing is left to
        carry, so the whole actuation is skipped, and it used to take this call
        with it: the one call a retry needed to re-make was the one call it
        would not.

        Only ever for a plate THIS move set down. A plate an operator carried
        was never handed over by an arm, and the hooks behind this call open
        doors and park gantries, so making them on somebody's hand-placed plate
        moves hardware for an arrival that already happened.
        """
        self._refuse_a_hand_in_the_machine(source_too=False)
        await self._action.target.notify_placed(
            self._action.labware, self._action.transporter,
        )
        self._action.set_target_still_to_be_told(False)

    async def _make_room_at_both_ends(self) -> None:
        """Wait for both devices to be able to take an arm, before the arm commits.

        Outside the mover's lock and before the pick, so a device that is
        minutes or hours from ready costs neither the jaws nor the arm. A slot
        reservation says the site is not claimed; it does not say the device
        behind it will let an arm in.

        The gates inside ``_actuate`` stay. A device ready now can be busy again
        by the time the arm gets there, and those are the ones the actuation
        depends on. This one only decides whether to commit.

        Skipped when there is nothing left to commit to: this move's plate is
        already in the jaws, so the retry owes a place rather than a wait, or
        the move is already done. A mover carrying somebody else's plate is
        neither. That is an ordinary move in flight, and skipping on it would
        skip the wait for every move queued behind one -- which is when it is
        needed most.

        The refusals run first, before anything is asked to move: parking a
        device an operator has taken moves a gantry with their hands in it, and
        parking for a move that was never going to run moves one for nothing.
        """
        mover = self._action.transporter
        if self._jaws_hold_our_labware() or self._move_is_already_done():
            return
        self._refuse_before_anything_moves(source_too=True)
        await clear_the_deck_for(self._action.source, mover)
        await clear_the_deck_for(self._action.target, mover)

    def _refuse_before_anything_moves(self, *, source_too: bool) -> None:
        """The three refusals that must land before an arm or a gantry moves.

        Asked before the arm commits and again under the lock, because none of
        them is lock-synchronized with the reservation. In order: a stranger on
        the target, then a hand in the machine, then a plate that is no longer
        on its source. The hand outranks the plate because while somebody is
        hands-on the ledger is expected to lag anyway.

        The source is skipped on a retry that already holds the plate: no pick
        will run, so a flag on the source is not interference.
        """
        occupant = self._action.target.labware
        if occupant is not None:
            raise ValueError(
                f"Target location {self._action.target.position_id} is occupied "
                f"by {occupant.name}"
            )
        self._refuse_a_hand_in_the_machine(source_too=source_too)
        if source_too:
            self._require_labware_at_source()

    def _refuse_a_hand_in_the_machine(self, *, source_too: bool) -> None:
        """Refuse while an operator has taken a device this move would touch.

        Its own method because every path that reaches a device owes it, not
        only the paths that send the arm.
        """
        locations = (
            (self._action.source, self._action.target) if source_too
            else (self._action.target,)
        )
        for location in locations:
            blocked = device_under_external_control(location)
            if blocked is not None:
                raise DeviceUnderExternalControlError(blocked.name)
        if self._action.transporter.under_external_control:
            raise DeviceUnderExternalControlError(self._action.transporter.name)

    async def _hold_the_slot_the_plate_is_still_in(self) -> LocationReservation | None:
        """Take the source for the length of the actuation, when the plate has
        not left it yet. Why, and why it cannot deadlock, is on
        ``MoveHandler.hold_the_source``.
        """
        if self._action.transporter.pick_moves_the_plate:
            return None
        return await self._slot_holder.hold_the_source(
            self._context.thread_id, self._action.labware, self._action.source,
        )

    async def _actuate(self) -> None:
        """Carry the labware to the target. Runs under the mover's lock."""
        # If the transporter already holds our labware (from a previous
        # failed attempt), skip pick and go straight to place.
        already_picked = self._jaws_hold_our_labware()
        self._refuse_before_anything_moves(source_too=not already_picked)

        mover = self._action.transporter
        if already_picked:
            await self._place_at_target(mover)
        else:
            self._fire(ActionEvent.MOVE_PREPARING)
            # Gated before the fingers enter: a handler mid-transfer
            # refuses to move off its deck and this waits for it.
            await clear_the_deck_for(self._action.source, mover)
            await self._action.source.prepare_for_pick(self._action.labware, mover)
            await self._action.target.prepare_for_place(self._action.labware, mover)

            self._fire(ActionEvent.MOVE_PICKING)
            await mover.pick(self._action.source)
            # Before notify_picked, which is a device call that can fail: the
            # plate is in the jaws the instant pick returns.
            await self._record_picked_up(mover)
            try:
                await self._action.source.notify_picked(self._action.labware, mover)
                await self._place_at_target(mover)
            except Exception:
                await self._undo_a_pick_that_moved_nothing(mover)
                raise
        await self._record_set_down(mover)
        self._action.set_target_still_to_be_told(True)
        await self._action.target.notify_placed(self._action.labware, mover)
        self._action.set_target_still_to_be_told(False)

    async def _place_at_target(self, mover: TransporterBase) -> None:
        """Everything up to and including the actuation that sets it down.

        Kept apart from the records that follow so the rollback around it can
        cover the actuation and nothing after: once the place has returned the
        plate is at the target, and putting the record back at the source would
        be the lie in the other direction.
        """
        await clear_the_deck_for(self._action.target, mover)
        self._fire(ActionEvent.MOVE_PLACING)
        await mover.place(self._action.target)

    async def _undo_a_pick_that_moved_nothing(self, mover: TransporterBase) -> None:
        """Put the labware back when the mover never lifted it.

        A mover whose driver carries the plate in one call has touched nothing
        until that call returns, so a refused place leaves the plate on its
        slot. Keeping the jaws record would strand a hold for a pick that never
        happened: the next plate finds the jaws full, and a deck reconcile has
        a labware at no site to project.

        A raise after that call already moved the plate is not distinguishable
        from a refusal, here or anywhere else. The source is the better guess of
        the two: the driver reports the same thing as its own interrupted move,
        and a plate this mover left held is one its jaws cannot be trusted to
        still have. Only an action that found the plate ALREADY in the jaws
        leaves that record alone, because it did not put it there.
        """
        if mover.pick_moves_the_plate:
            return
        try:
            await self._labware_placer.put_back(
                self._action.labware, self._action.source, mover.gripper_location,
            )
        except Exception:
            # The source refilled while the move was in flight, which the
            # handoff site does on every pipelined run. There is nowhere to put
            # the plate back, so the hold stands and the caller's own error is
            # the one the operator needs.
            orca_logger.exception(
                "%s: could not put %s back on %s after a failed move; it stays "
                "recorded in the jaws and an operator has to say where it is.",
                mover.name, self._action.labware.name,
                self._action.source.position_id,
            )

    async def _record_picked_up(self, mover: TransporterBase) -> None:
        """The move says what happened; the chokepoint records it. The mover
        used to keep an answer of its own, which is what this removes."""
        await self._labware_placer.picked_up(
            self._action.labware, mover.gripper_location,
        )

    async def _record_set_down(self, mover: TransporterBase) -> None:
        await self._labware_placer.set_down(
            self._action.labware, self._action.target, mover.gripper_location,
        )

    async def execute(self) -> None:
        if self.status == ActionStatus.COMPLETED:
            return
        if self.status == ActionStatus.ERRORED:
            raise ValueError("Action has errored, cannot execute")
        try:
            await self._execute_action()
        except Exception as e:
            self._fire(ActionEvent.ACTION_ERRORED)
            raise e
        self._fire(ActionEvent.ACTION_COMPLETED)

    @property
    def id(self) -> str:
        return self._action.id

    @property
    def labware(self) -> LabwareInstance:
        return self._action.labware

    @property
    def source(self) -> Location:
        return self._action.source

    @property
    def target(self) -> Location:
        return self._action.target

    @property
    def transporter(self) -> TransporterBase:
        return self._action.transporter

    @property
    def release_reservation_on_place(self) -> bool:
        return self._action.release_reservation_on_place

    @property
    def reservation(self) -> LocationReservation:
        return self._action.reservation

    def set_reservation(self, reservation: LocationReservation) -> None:
        self._action.set_reservation(reservation)

    def set_release_reservation_on_place(self, release: bool) -> None:
        self._action.set_release_reservation_on_place(release)

    @property
    def target_still_to_be_told(self) -> bool:
        return self._action.target_still_to_be_told

    def set_target_still_to_be_told(self, owed: bool) -> None:
        self._action.set_target_still_to_be_told(owed)


class MoveAction(IMoveAction):
    def __init__(self,
                 labware: LabwareInstance,
                 source: Location,
                 target: Location,
                 transporter: TransporterBase,
                 onward_seats: Sequence[Location] = (),
                 terminal_candidates: Sequence[Location] = ()):
        self._id = str(uuid.uuid4())
        self._labware = labware
        self._source = source
        self._target = target
        self._transporter = transporter
        self._release_reservation_on_place = True
        # The one reservation that is a plate arriving somewhere. The seats
        # and resting candidates below are options this route may not take.
        self._reservation = LocationReservation(
            self.target, self.labware,
            priority=ReservationPriority.MOVE_TARGET,
        )
        # Corridor boarding: the seat chain plus ANY ONE terminal resting
        # candidate is granted WITH the boarding.
        self._onward_seat_reservations = [
            LocationReservation(location, labware) for location in onward_seats
        ]
        self._terminal_reservations = [
            LocationReservation(location, labware) for location in terminal_candidates
        ]
        self._substituted_onward_ids: set[str] = set()
        self._shared_onward_ids: set[str] = set()
        self._target_still_to_be_told = False

    @property
    def id(self) -> str:
        return self._id

    @property
    def labware(self) -> LabwareInstance:
        return self._labware

    @property
    def source(self) -> Location:
        return self._source

    @property
    def target(self) -> Location:
        return self._target

    @property
    def transporter(self) -> TransporterBase:
        return self._transporter

    @property
    def target_still_to_be_told(self) -> bool:
        return self._target_still_to_be_told

    def set_target_still_to_be_told(self, owed: bool) -> None:
        self._target_still_to_be_told = owed

    @property
    def release_reservation_on_place(self) -> bool:
        return self._release_reservation_on_place

    @property
    def reservation(self) -> LocationReservation:
        return self._reservation

    @property
    def onward_seat_reservations(self) -> List[LocationReservation]:
        return list(self._onward_seat_reservations)

    @property
    def terminal_reservations(self) -> List[LocationReservation]:
        return list(self._terminal_reservations)

    @property
    def has_corridor_run(self) -> bool:
        return bool(self._onward_seat_reservations) or bool(self._terminal_reservations)

    @property
    def owned_onward_reservations(self) -> List[LocationReservation]:
        """Onward entries this move may attempt, release, and clear itself.

        Excludes entries substituted with an action-owned reservation (the
        action's lifecycle owns those) and entries shared with a sibling
        candidate move (the first carrier owns those); attempting, releasing,
        or clearing either from here would tear a hold out from under its
        owner.
        """
        return [
            r for r in [*self._onward_seat_reservations, *self._terminal_reservations]
            if r.id not in self._substituted_onward_ids
            and r.id not in self._shared_onward_ids
        ]

    @property
    def shared_onward_reservations(self) -> List[LocationReservation]:
        """Onward entries borrowed from a sibling candidate move (one
        reservation object per position across a collection)."""
        return [
            r for r in [*self._onward_seat_reservations, *self._terminal_reservations]
            if r.id in self._shared_onward_ids
        ]

    def substitute_onward_reservation(
        self, position_id: str, reservation: LocationReservation
    ) -> None:
        if self._replace_onward(position_id, reservation):
            self._substituted_onward_ids.add(reservation.id)

    def share_onward_reservation(
        self, position_id: str, reservation: LocationReservation
    ) -> None:
        """Borrow a sibling candidate move's reservation for ``position_id``.

        Sibling moves in one collection sharing a position must reference ONE
        reservation object; separate objects displace each other at attempt
        time and the collection rejects a free route forever.
        """
        if self._replace_onward(position_id, reservation):
            self._shared_onward_ids.add(reservation.id)

    def _replace_onward(
        self, position_id: str, reservation: LocationReservation
    ) -> bool:
        for reservations in (self._onward_seat_reservations, self._terminal_reservations):
            for index, existing in enumerate(reservations):
                if existing.requested_location.position_id == position_id:
                    reservations[index] = reservation
                    return True
        return False

    def is_substituted_onward(self, reservation: LocationReservation) -> bool:
        return reservation.id in self._substituted_onward_ids

    def set_reservation(self, reservation: LocationReservation) -> None:
        self._reservation = reservation

    def set_release_reservation_on_place(self, release: bool) -> None:
        self._release_reservation_on_place = release

    def executable(
        self,
        status_manager: StatusManager,
        context: ThreadExecutionContext,
        labware_location_service: ILabwareLocationService,
        labware_placer: LabwarePlacer,
        slot_holder: IHoldsTheSourceSlot,
    ) -> ExecutableMoveAction:
        """Two-stage lifecycle parallel to the LocationAction chain (W2):
        MoveAction (data) -> ExecutableMoveAction (execution-context bound).
        No AssignedMoveAction stage -- moves carry no labware-assignment slots."""
        return ExecutableMoveAction(
            status_manager, context, self, labware_location_service, labware_placer,
            slot_holder,
        )