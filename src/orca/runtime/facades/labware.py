"""LabwareFacade: external UI surface over `ILabwareService` + `ILabwareLocationService`.

The labware service owns identity (id + barcode). The location service
owns position + history. This facade bridges them so UIs talk to one object.

All methods are async to match the labware service's async contract.

Guard scope for 7.5a: the `@dangerous` decorator + confirmation prompt is the
primary UX guard. Engine-level refusals (thread-status, reserved-target,
dependent-relationship) are best-effort and documented where they stop short
of the plan's full guard set -- follow-up work tightens them.
"""

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch

from orca.state.unsettled import SettleNoun, set_verb
from orca.state.confirm_refusals import (
    NothingToConfirmError, WorkLostError,
)
from orca.resource_models.move_parameters import reject_contradiction

from orca.resource_models.adhoc_labware import LabwareHasNoModel
from orca.resource_models.labware_placement import Arrival, PositionNotSavedError, Relocation, TurnPerLabware
from orca.resource_models.labware import (
    LabwareInstance,
    LabwareTemplate,
    TipRackInstance,
    TipRackTemplate,
)
from orca.resource_models.labware_location_service import ArrivalMechanism, ILabwareLocationService
from orca.state.placement import PlacementState
from orca.state.contents import ContentsResolution
from orca.state.projections import unsettled_gaps
from orca.state.records import GAPS_THAT_LOST_WORK
from orca.resource_models.location import Location
from orca.resource_models.transporter_base import TransporterBase
from orca.resource_models.resources import ILabwareStateHolder
from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.execution import Execution
from orca.resource_models.labware_service_interface import ILabwareService
from orca.runtime.labware_holds import holds_labware, refusal_to_change_position
from orca.runtime.stored_labware import adopt_stored_labware
from orca.runtime.errors import (
    ActiveExecutionRefusedError,
    BarcodeInUseError,
    LabwareNotFoundError,
    LabwareInUseByThreadError,
    LocationReservedError,
    MoverHoldsNothingError,
    UnknownLocationError,
)
from orca.runtime.facade_interfaces import (
    ClearSubmissionResult,
    ILabwareFacade,
    MoverHoldRelease,
)
from orca.runtime.runtime_interface import (
    IExecutionIterator,
)
from orca.workflow_models.errors import SpawnIncompatibleError
from orca.runtime.status_models import (
    LabwareSnapshot,
    LocationEvent,
    TipStateSnapshot,
    WellVolumesSnapshot,
)
from orca.resource_models.device_error import SlotOccupiedError
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
    ReservationPriority,
)
from orca.system.system_interface import ISystem
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.status_enums import LabwareThreadStatus

orca_logger = logging.getLogger("orca")


def _status_of(thread: ExecutingLabwareThread) -> LabwareThreadStatus | None:
    """None when it cannot be read: status is filed under the labware id and a LIVE place swaps it."""
    try:
        return thread.status
    except KeyError:
        return None


def _barcode_or_none(barcode: str | None) -> str | None:
    """A blank barcode is no barcode, which any number of labware may have."""
    return (barcode or "").strip() or None


@dataclass(frozen=True)
class ReservationHolder:
    """Who has a position and why, for a refusal that has to explain itself.

    ``labware_name``/``labware_at`` describe the plate the holder is bringing;
    ``awaiting_operator`` says the holder is instead waiting for a person to
    place one, in which case there is no plate to wait for.
    """

    reservation_id: str
    holder_thread_id: str | None
    holder_thread_name: str | None
    labware_name: str | None
    labware_at: str | None
    awaiting_operator: bool


class LabwareFacade(ILabwareFacade):
    """Concrete LabwareFacade implementation.

    Every verb here that reaches a device states its run-mode base, the same
    way the device verbs do. These calls arrive from an operator, and an
    operator's call is never inside an execution, so the ambient mode is the
    metadata fallback: without a stated base the deck projection lands on the
    device's sim-world driver and the instrument is never told. A device
    set to sim still resolves sim.
    """

    def __init__(
        self,
        system: ISystem,
        labware_service: ILabwareService,
        execution_iterator: IExecutionIterator,
    ) -> None:
        self._system = system
        self._labware_service = labware_service
        self._execution_iterator = execution_iterator
        self._location_service: ILabwareLocationService = system.labware_location_service
        self._contents = system.labware_contents
        # One position statement per labware at a time, from reading where it is to writing where it now is.
        self._statements = TurnPerLabware()

    # -- Reads ---------------------------------------------------------------

    async def _find_by_id(self, labware_id: str) -> LabwareInstance | None:
        """Resolve a labware id to the instance the engine is holding.

        The engine registry answers first. A store read may REBUILD an
        instance from a persisted row rather than return the live object
        (a Db-backed store does), and handing that copy to a mutating
        caller silently breaks every downstream identity check: the position
        holders keep the plate, the active-thread refusal never fires, and
        disposal reaches nothing.

        The store stays as the fallback for a row the engine has no object
        for -- a boot-time orphan. Both directions are needed: the store only
        sees instances routed through ``register()`` / ``edit_barcode()``,
        while ``system.labwares`` also holds everything auto-spawned.
        """
        for instance in self._system.labwares:
            if instance.id == labware_id:
                return instance
        return await self._labware_service.get_by_id(labware_id)

    async def get_by_id(self, labware_id: str) -> LabwareSnapshot:
        instance = await self._find_by_id(labware_id)
        if instance is None:
            raise KeyError(f"No labware with id '{labware_id}'")
        return await self._snapshot(instance)

    async def _refuse_a_barcode_held_by_another(
        self, barcode: str | None, labware_id: str | None,
    ) -> None:
        if barcode is None:
            return
        holder = await self._labware_service.get_by_barcode(barcode)
        if holder is None:
            holder = next(
                (lw for lw in self._system.labwares if lw.barcode == barcode), None,
            )
        if holder is not None and holder.id != labware_id:
            raise BarcodeInUseError(barcode, holder.id)

    async def get_by_barcode(self, barcode: str) -> LabwareSnapshot:
        stored = await self._labware_service.get_by_barcode(barcode)
        if stored is not None:
            return await self._snapshot(stored)
        # Fallback for instances the engine spawned without routing through
        # the store (e.g. tests that didn't register via the store).
        for instance in self._system.labwares:
            if instance.barcode == barcode:
                return await self._snapshot(instance)
        raise KeyError(f"No labware with barcode '{barcode}'")

    async def list_all(self) -> list[LabwareSnapshot]:
        # The system's in-memory labware registry is the canonical source for
        # labware visible to the running system. Items registered only in the
        # async store (e.g. via register()) also appear because they are
        # subsequently added to `system.labwares` when the engine hands off.
        snapshots: list[LabwareSnapshot] = []
        for instance in self._system.labwares:
            snapshots.append(await self._snapshot(instance))
        return snapshots

    async def get_history(self, labware_id: str) -> list[LocationEvent]:
        instance = await self._find_by_id(labware_id)
        if instance is None:
            raise KeyError(f"No labware with id '{labware_id}'")
        # Prefer the durable store audit trail when present: it survives
        # runtime rebuilds and accumulates the full edit/reset history,
        # whereas ``InMemoryLabwareLocationService`` is per-runtime and
        # ``reset()`` clears its internal log. Fall back to the in-process
        # location service for runtimes that haven't routed updates
        # through the store yet.
        store_events = await self._labware_service.get_location_history(labware_id)
        if store_events:
            return [
                LocationEvent(sequence=i, position_id=name, timestamp=ts)
                for i, (name, _previous, ts) in enumerate(store_events)
            ]
        try:
            history = self._location_service.get_history(instance)
        except KeyError:
            return []
        return [
            LocationEvent(sequence=i, position_id=loc.name, timestamp=ts)
            for i, (loc, ts) in enumerate(history.get_history_with_timestamps())
        ]

    async def get_well_volumes(self, labware_id: str) -> WellVolumesSnapshot:
        """The folded per-well volumes plus how well the record knows them."""
        instance = await self._find_by_id(labware_id)
        if instance is None:
            raise KeyError(f"No labware with id '{labware_id}'")
        contents = await self._contents.of(instance.ref)
        return WellVolumesSnapshot(
            labware_id=labware_id,
            volumes=contents.volumes,
            provenance=contents.provenance,
        )

    # -- Writes --------------------------------------------------------------

    @dangerous(
        name="labware.edit_location",
        level=DangerLevel.PHYSICAL,
        message="Manually set labware '{labware_id}' location to '{location}'. "
                "No transporter invoked; you are asserting the plate is physically "
                "there already. A thread carrying this labware drops the move it "
                "planned and plans a fresh one from where you say the labware is.",
        requires_reason=True,
    )
    async def edit_location(
        self, labware_id: str, location: str,
        *, reason: str | None = None,
    ) -> None:
        """Operator-asserted location change.

        The move goes through the placement chokepoint (``system.labware_placer``)
        as ONE step: both holders (slot + bridge loaded-list) move, then the
        decks are projected, then the position ledger. Two holders and one
        projection is why it cannot be a place followed by a clear: a deck
        projection is deck-wide and reads the slots, so projecting the target
        while the source still holds the labware shows it at two sites at once,
        which is an ordinary move between two slots of one liquid handler.

        The placer also saves the position through the labware service; the
        facade never writes it, so each placement lands exactly one history row.
        A busy target or an unreachable device fails with both holders left
        where they were, so the operator's own re-issue is the repair. A save
        that fails reaches the caller.

        Same location: nothing moves, and the placer saves the position again,
        which refreshes its timestamp.
        A labware that is only EXPECTED somewhere has no source, so the operator
        asserting it is at that same location is a real placement and takes the
        full path -- skipping it would persist a position for a plate nothing
        has put down.

        Two things make a target impossible rather than merely awkward, and
        both refuse before anything is written: another labware is already
        there, and another thread has the position reserved. A thread moving the
        labware or running an action on it also refuses. Any other thread carrying
        it is told to plan its move again from where the operator says it now is.
        """
        del reason  # consumed by @dangerous audit; facade ignores body
        await self._assert_position(labware_id, location)

    async def _assert_position(self, labware_id: str, location: str) -> None:
        """The body of ``edit_location``, shared with the mover-hold release.

        Separate from the entry point so the other verb reuses the placement
        without re-entering an audited method: one operator action writes one
        audit row.
        """
        async with self._statements.turn(labware_id):
            await self._assert_position_now(labware_id, location)

    async def _assert_position_now(self, labware_id: str, location: str) -> None:
        instance, target_location, carrier = await self._prepare_placement(
            labware_id, location,
        )
        current_location = self._location_service.present_location(instance)
        if current_location is not target_location:
            if current_location is None:
                await self._system.labware_placer.arrived(instance, target_location, Arrival.STATED)
            else:
                await self._system.labware_placer.relocate(
                    instance, current_location, target_location, Relocation.STATED,
                )
            self._tell_carrier_to_look_again(carrier)
        else:
            await self._system.labware_placer.confirmed(instance, target_location)

    async def _prepare_placement(
        self, labware_id: str, location: str,
    ) -> tuple[LabwareInstance, Location, ExecutingLabwareThread | None]:
        """Resolve what an operator named and refuse it if nothing could go
        there. Shared by the two verbs that state a position, so neither can
        become the way round the other's guard."""
        instance = await self._find_by_id(labware_id)
        if instance is None:
            raise LabwareNotFoundError(labware_id)
        try:
            target = self._system.system_map.resolve_placement_location(location)
        except KeyError as exc:
            raise UnknownLocationError(location) from exc
        carrier = self._active_thread_for(instance)
        self._refuse_while_a_thread_is_using_it(instance, carrier)
        self._refuse_impossible_placement(instance, target, carrier)
        if not any(held is instance for held in self._system.labwares):
            instance = await adopt_stored_labware(self._system, instance)
        return instance, target, carrier

    def _refuse_while_a_thread_is_using_it(
        self, instance: LabwareInstance, carrier: ExecutingLabwareThread | None,
    ) -> None:
        if carrier is None:
            return
        refusal = refusal_to_change_position(_status_of(carrier), instance.name, carrier.name)
        if refusal is not None:
            raise LabwareInUseByThreadError(refusal)

    @staticmethod
    def _tell_carrier_to_look_again(
        carrier: ExecutingLabwareThread | None,
    ) -> None:
        """Called last, and only once the position really changed: a thread
        told to look again gives up a granted move to do it."""
        if carrier is not None:
            carrier.notify_labware_moved()

    def _refuse_impossible_placement(
        self, instance: LabwareInstance, target: Location,
        carrier: ExecutingLabwareThread | None,
    ) -> None:
        """Refuse a target nothing could be put down at.

        Runs before the placer so a refusal leaves every holder untouched; the
        placer's own ``SlotOccupiedError`` stays as the backstop for a slot that
        fills in between.
        """
        occupant = target.labware
        if occupant is not None and occupant is not instance:
            raise SlotOccupiedError(
                target.position_id, occupant.name, occupant.template_name,
            )
        reservation = self._reservation_at(target.position_id)
        if reservation is None:
            return
        if carrier is not None and reservation.holder_thread_id == carrier.id:
            # The thread carrying this labware reserved the target itself, which
            # is what an operator finishing that move by hand is doing.
            return
        raise LocationReservedError(
            target.position_id,
            reservation.reservation_id,
            reservation.holder_thread_id,
            reservation.holder_thread_name,
            inbound_labware=reservation.labware_name,
            inbound_from=reservation.labware_at,
            awaiting_operator=reservation.awaiting_operator,
        )

    def _reservation_at(self, position_id: str) -> ReservationHolder | None:
        """Who holds ``position_id``, and enough about them to explain it.

        The coordinator is system-wide, so any running execution lists every
        reservation, but only its OWN threads can be named -- so the names are
        collected across all of them before the lookup, or a second execution's
        hold reports a bare id nobody can act on. A reservation no thread owns
        reports Nones: held, but not by anyone to go and look at.

        The labware and the tier come off the reservation itself rather than
        being guessed from the holder's status, so a refusal cannot disagree
        with the rule that produced it.
        """
        names: dict[str, str] = {}
        found: tuple[str, str | None] | None = None
        reservation: LocationReservation | None = None
        for execution in self._iter_active_executions():
            ew = execution.executing_workflow
            if ew is None:
                continue
            names.update({t.id: t.name for t in ew.threads})
            if found is None:
                found = next(
                    (
                        (reservation_id, thread_id)
                        for held, reservation_id, thread_id
                        in ew.get_active_reservations()
                        if held == position_id
                    ),
                    None,
                )
                if found is not None:
                    reservation = ew.get_reservation_at(position_id)
        if found is None:
            return None
        reservation_id, thread_id = found
        labware = reservation.labware if reservation is not None else None
        at = self._location_service.present_location(labware) if labware is not None else None
        return ReservationHolder(
            reservation_id=reservation_id,
            holder_thread_id=thread_id,
            holder_thread_name=names.get(thread_id) if thread_id else None,
            labware_name=labware.name if labware is not None else None,
            labware_at=at.position_id if at is not None else None,
            awaiting_operator=(
                reservation is not None
                and reservation.priority is ReservationPriority.AWAITING_OPERATOR
            ),
        )

    @dangerous(
        name="labware.edit_barcode",
        level=DangerLevel.OPERATOR,
        message="Change labware '{labware_id}' barcode to '{new_barcode}'. "
                "In-flight actions that captured the old barcode in their context "
                "will not see the change; join lookups by barcode will use the new value.",
    )
    async def edit_barcode(
        self, labware_id: str, new_barcode: str,
        *, reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
        instance = await self._find_by_id(labware_id)
        if instance is None:
            raise KeyError(f"No labware with id '{labware_id}'")
        barcode = _barcode_or_none(new_barcode)
        await self._refuse_a_barcode_held_by_another(barcode, instance.id)
        # The store indexes by barcode, so re-register to refresh the index.
        previous = instance.barcode
        instance.barcode = barcode
        try:
            await self._labware_service.register(instance)
        except BarcodeInUseError:
            instance.barcode = previous
            raise

    @dangerous(
        name="labware.set_carry_override",
        level=DangerLevel.PHYSICAL,
        message="Change how labware '{labware_id}' is carried. This wins over "
                "the labware type's grip profile, the position's numbers and "
                "the arm's defaults, and it applies to this one piece of "
                "labware until it is cleared. It takes effect on the next move.",
    )
    async def set_carry_override(
        self, labware_id: str,
        patch: MoveParameterPatch,
        clear: Sequence[MoveParameterField] = (),
        *, reason: str | None = None,
    ) -> MoveParameterPatch:
        """Say how this one piece of labware is carried, over every other layer.

        For what is true of this object and nothing else: a lid fitted at the
        sealer, a plate filled to the brim. Anything true of the labware TYPE
        belongs in its grip profile, where the next plate of that type inherits
        it instead of being measured again.

        Setting and clearing land in one write, so a move between two edits
        never resolves against a mixture neither of them intended.
        """
        del reason  # consumed by @dangerous audit
        reject_contradiction(patch, clear)
        instance = await self._require(labware_id)
        instance.carry_with(**{
            **patch.model_dump(exclude_none=True),
            **dict.fromkeys(clear),
        })
        await self._labware_service.register(instance)
        return instance.carry_override

    @dangerous(
        name="labware.clear_carry_override",
        level=DangerLevel.PHYSICAL,
        message="Carry labware '{labware_id}' the way anything else of its type "
                "is carried. If it was being carried higher or slower for a "
                "reason, that reason stops applying on the next move.",
    )
    async def clear_carry_override(self, labware_id: str, *, reason: str | None = None) -> None:
        """Drop the override; this labware resolves like any other again."""
        del reason  # consumed by @dangerous audit
        instance = await self._require(labware_id)
        instance.carry_normally()
        await self._labware_service.register(instance)

    async def _require(self, labware_id: str) -> LabwareInstance:
        instance = await self._find_by_id(labware_id)
        if instance is None:
            raise KeyError(f"No labware with id '{labware_id}'")
        return instance

    @dangerous(
        name="labware.reset_location",
        level=DangerLevel.PHYSICAL,
        message="Clear location history for '{labware_id}' and set initial position "
                "to '{location}'. Only safe before any thread has moved this labware; "
                "at runtime this throws that history away, and a thread carrying "
                "the labware plans its move again from the position you give.",
        requires_reason=True,
    )
    async def reset_location(
        self, labware_id: str, location: str,
        *, reason: str | None = None,
    ) -> None:
        """Say where the labware is AND forget everywhere it has been.

        Refused for the same reasons ``edit_location`` is, and a thread
        carrying the labware is told to plan again the same way: a position an
        operator cannot state one way cannot be stated the other way either.

        Two things are its own. The history is wiped, which is why this stays
        the verb for before a thread has moved the labware rather than a
        synonym. And the placement goes through the chokepoint even when the
        labware is already there, because re-establishing a position is the
        point, so the deck is told again. Persistence rides the chokepoint's
        listener; nothing writes the store's location directly. A labware
        presently somewhere else moves rather than being copied: leaving the
        old holder would put one plate at two sites.
        """
        del reason  # consumed by @dangerous audit; facade ignores body
        async with self._statements.turn(labware_id):
            await self._restate_position(labware_id, location)

    async def _restate_position(self, labware_id: str, location: str) -> None:
        instance, target_location, carrier = await self._prepare_placement(
            labware_id, location,
        )
        current_location = self._location_service.present_location(instance)
        # Unlike edit, a restatement still goes through the placer: this verb
        # exists to re-establish a position, so the deck is told again.
        if current_location is None or current_location is target_location:
            await self._system.labware_placer.arrived(instance, target_location, Arrival.RESTATED)
        else:
            await self._system.labware_placer.relocate(
                instance, current_location, target_location, Relocation.RESTATED,
            )
        if current_location is not target_location:
            self._tell_carrier_to_look_again(carrier)

    @dangerous(
        name="labware.register",
        level=DangerLevel.OPERATOR,
        message="Register a new labware instance (template_name={template_name}, "
                "labware_type={labware_type}) with barcode={barcode} and starting "
                "location={location}. One of the two names the labware and the "
                "other reads None. The new instance becomes visible to subsequent "
                "actions that target its template.",
    )
    async def register(
        self, template_name: str | None = None, *,
        labware_type: str | None = None,
        barcode: str | None = None,
        location: str | None = None,
        reason: str | None = None,
    ) -> LabwareSnapshot:
        """Record labware an operator has put down, optionally binding it to a
        physical Location slot.

        Say what it is either way round: ``template_name`` for labware the
        deployment package declares, or ``labware_type`` for a catalog
        definition nothing declared, which derives an ad-hoc template on the
        spot (see ``orca.resource_models.adhoc_labware``). Exactly one of the
        two. The ad-hoc route is what lets an operator introduce labware the
        workflow author never anticipated instead of editing code.

        When a thread is parked waiting for labware of this template at this
        location, the operator has just placed THAT labware, not a second piece
        of it -- so the expectation is adopted and its instance is what arrives.
        Minting a fresh instance would leave the engine holding one id while the
        operator surfaces show another. Only a slot
        nothing is waiting on mints a new instance.

        When ``location`` is provided, placement routes through the system
        placement chokepoint (``system.labware_placer``): the slot, the staging-
        bridge loaded-list, the position ledger, and the LH driver deck
        projection are all written together, so a deck-site child registers
        coherently (the pick path finds it on the bridge loaded-list).

        Failure modes: a slot awaited for a different template is refused with
        ``SpawnIncompatibleError`` before anything is written, so the operator
        learns at their own call rather than through a thread failing behind
        them. A slot another thread has reserved is refused with
        ``LocationReservedError``, the same as the two verbs that state a
        position, naming the thread to clear. An occupied slot raises
        ``SlotOccupiedError``. A barcode another labware holds raises
        ``BarcodeInUseError``; a removed labware still holds its barcode, and a
        blank barcode counts as none. All four refuse before any registry
        mutation, so operator state never lands partially-registered and the
        same call is the fix once the cause is.
        """
        del reason  # consumed by @dangerous audit
        barcode = _barcode_or_none(barcode)
        template_name = await self._resolve_template_name(template_name, labware_type)
        target_location = None
        if location is not None:
            try:
                target_location = self._system.system_map.resolve_placement_location(location)
            except KeyError as exc:
                raise UnknownLocationError(location) from exc
        adopted = self._adopt_expected(target_location, template_name)
        await self._refuse_a_barcode_held_by_another(
            barcode, adopted.id if adopted is not None else None,
        )
        if adopted is not None:
            instance = adopted
        else:
            template = self._system.get_labware_template(template_name)
            try:
                instance = await template.create_instance()
            except (RuntimeError, NotImplementedError) as exc:
                # A declared template reaches the same factory resolver as a
                # derived one, and its raises shape into a 500 with the reason
                # dropped. Same refusal either way round.
                raise LabwareHasNoModel(
                    f"template {template_name!r} names labware type "
                    f"{template.labware_type!r}, which cheshire-drivers exposes "
                    f"no model for, so no instance can be made: {exc}"
                ) from exc
        if target_location is not None:
            # The thread being waited on holds the claim on the slot it is
            # waiting at, so it is the one thread this register must not be
            # refused for: filling that slot is what it asked for.
            self._refuse_impossible_placement(
                instance, target_location,
                self._active_thread_for(adopted) if adopted is not None else None,
            )
        if barcode is not None:
            instance.barcode = barcode
        # Placing projects onto the driver deck, and a driver asked for a tip
        # layout nobody has written yet fills the silence with a full rack.
        await instance.enter_record(self._contents)
        if target_location is not None:
            # Fail-fast: the chokepoint writes the slot first and raises
            # SlotOccupiedError on a busy slot, before the registry mutations below.
            await self._system.labware_placer.arrived(instance, target_location, Arrival.PLACED)
        if adopted is None:
            self._system.add_labware(instance)
            await self._labware_service.register(instance)
        return await self._snapshot(instance)

    async def _resolve_template_name(
        self, template_name: str | None, labware_type: str | None,
    ) -> str:
        """The template to mint from, whichever way the caller named the labware.

        A catalog type derives its ad-hoc template here rather than at the call
        site, so every surface that registers labware lands on the same template
        for the same type and the deck projection has a ``catalog_ref`` to use.
        """
        # A blank string is how a client that always sends every field says
        # "unset", so it reads the same as absent rather than as a name.
        template_name = (template_name or "").strip() or None
        labware_type = (labware_type or "").strip() or None
        if template_name is not None and labware_type is not None:
            raise ValueError(
                f"register was given both template_name={template_name!r} and "
                f"labware_type={labware_type!r}; name the labware one way. A "
                f"declared template already fixes its labware type."
            )
        if template_name is not None:
            return template_name
        if labware_type is None:
            raise ValueError(
                "register needs template_name (labware the deployment package "
                "declares) or labware_type (a catalog definition it does not)."
            )
        template = await self._system.ensure_adhoc_labware_template(labware_type)
        return template.name

    def _adopt_expected(
        self, target_location: Location | None, template_name: str,
    ) -> LabwareInstance | None:
        """The labware a thread is already waiting for at this slot, if any.

        Refuses a template the waiter cannot use rather than placing it and
        leaving the thread parked next to labware it will never accept.
        """
        if target_location is None:
            return None
        position_id = target_location.position_id
        match = self._location_service.expected_at(
            position_id, ArrivalMechanism.MANUAL_PLACE, template_name,
        )
        if match is not None:
            return match
        awaited = self._location_service.expected_at(
            position_id, ArrivalMechanism.MANUAL_PLACE,
        )
        if awaited is not None:
            raise SpawnIncompatibleError(
                location=target_location.name,
                expected_template=awaited.template_name,
                actual_template=template_name,
            )
        return None

    @dangerous(
        name="labware.set_volume",
        level=DangerLevel.PHYSICAL,
        message="Manually set per-well volumes on labware '{labware_id}': {volumes}. "
                "You are asserting the wells physically hold these amounts; the value "
                "overrides tracked volume (absolute) and seeds the driver tracker if "
                "the labware is on-deck. Overfill beyond a well's capacity is rejected.",
        requires_reason=True,
    )
    async def set_well_volumes(
        self, labware_id: str, volumes: dict[str, float],
        *, reason: str | None = None,
    ) -> None:
        """Operator-asserted absolute per-well volumes.

        Writes a SET_VOLUME ledger record (the authority); the fold treats it
        as an absolute overwrite of the named wells. Seeding is ledger-first,
        so an off-deck set survives to the next placement; an on-deck set is
        pushed to the driver tracker immediately via the deck reconcile. The
        reason rides the ``@dangerous`` audit trail, never the ledger record.
        """
        del reason  # consumed by @dangerous audit; facade ignores body
        instance = await self._find_by_id(labware_id)
        if instance is None:
            raise KeyError(f"No labware with id '{labware_id}'")
        for well_id, vol in volumes.items():
            if not instance.has_well(well_id):
                raise ValueError(f"labware '{labware_id}' has no well '{well_id}'")
            if vol < 0:
                raise ValueError(
                    f"volume for well '{well_id}' must be non-negative, got {vol}"
                )
            capacity = instance.well_capacity(well_id)
            if capacity is not None and vol > capacity:
                raise ValueError(
                    f"volume {vol} for well '{well_id}' exceeds capacity {capacity} "
                    f"on labware '{labware_id}'"
                )
        await self._contents.assert_volumes(instance.ref, dict(volumes))
        await self._push_well_state_if_on_deck(instance)

    @dangerous(
        name="labware.confirm_well_volumes",
        level=DangerLevel.OPERATOR,
        message="Confirm that labware '{labware_id}' physically matches its "
                "tracked well volumes. The volumes become an operator-asserted "
                "ledger baseline and the labware stops reading stale.",
    )
    async def confirm_well_volumes(
        self, labware_id: str,
        *, reason: str | None = None,
    ) -> None:
        """Operator agreement that the tracked volumes match the labware.

        The volume half of ``confirm_tip_state``, and needed for the same
        reason: a restart makes everything stale, and without a confirm the only
        way to settle a plate is to restate every well. Refused when the
        record holds no volumes, because agreeing with nothing would assert an
        empty plate nobody stated.
        """
        del reason  # consumed by @dangerous audit; facade ignores body
        instance = await self._require(labware_id)
        await self._refuse_a_confirm_the_record_cannot_back(instance, "well-volumes")
        contents = await self._contents.of(instance.ref)
        if not contents.volumes:
            raise NothingToConfirmError(
                f"nothing has ever said what is in labware '{labware_id}', so "
                f"there is nothing to confirm; state it with set_well_volumes",
                settle_with=set_verb("well-volumes"),
            )
        await self._contents.assert_volumes(instance.ref, dict(contents.volumes))

    async def _refuse_a_confirm_the_record_cannot_back(
        self, instance: LabwareInstance, noun: SettleNoun,
    ) -> None:
        """Confirming is agreeing with the record, and there are two states the
        record is known not to describe.

        A stop, a timeout or a failure that cut a command off leaves the read wrong by an amount nothing
        can state, and the labware looks right to anyone glancing at it. It is
        settled by measuring and stating.
        """
        ops = await self._contents.ops_of(instance.ref)
        if unsettled_gaps(ops, instance.name) & GAPS_THAT_LOST_WORK:
            raise WorkLostError(
                f"a stop, a timeout or a failure cut off a command that was working on "
                f"'{instance.id}', so the record is wrong by an amount nothing "
                f"can state and confirming it would mark that wrong number "
                f"checked; measure it and state it with set_{noun.replace('-', '_')}",
                settle_with=set_verb(noun),
            )

    async def resolve_contents(self, labware_id: str) -> ContentsResolution:
        instance = await self._require(labware_id)
        return await self._contents.resolve(
            instance.ref, declared_tip_count=self._declared_tip_count(instance),
        )

    def _declared_tip_count(self, instance: LabwareInstance) -> int | None:
        """How many tips the template said this rack starts with.

        Read here rather than in the ledger: a template is topology, and the
        ledger answers about the record. It rides the resolved read as a layer
        an operator can compare against, never as an answer about now.
        """
        template = instance.template
        if template is None:
            return None
        declared = template.declared_contents(instance)
        if declared is None or declared.tip_positions_present is None:
            return None
        return len(declared.tip_positions_present)

    async def get_tip_state(self, labware_id: str) -> TipStateSnapshot:
        """The rack's folded tip layout plus how well the record knows it."""
        instance = await self._require(labware_id)
        self._refuse_non_rack(instance)
        contents = await self._contents.of(instance.ref)
        return TipStateSnapshot(
            labware_id=labware_id,
            positions_present=contents.tip_positions_present,
            provenance=contents.provenance,
        )

    @dangerous(
        name="labware.set_tip_state",
        level=DangerLevel.PHYSICAL,
        message="Manually set the tip layout on rack '{labware_id}': tips at "
                "{tip_positions_present}, every other position empty. You are "
                "asserting the rack physically holds exactly this; the value "
                "overrides tracked tip state (absolute) and seeds the driver "
                "rack if the labware is on-deck.",
        requires_reason=True,
    )
    async def set_tip_state(
        self, labware_id: str, tip_positions_present: list[str],
        *, reason: str | None = None,
    ) -> None:
        """Operator-asserted absolute tip layout.

        Writes a SET_TIP_STATE ledger record (the authority); the fold treats
        it as an absolute overwrite. Clears the stale mark: an assertion of the
        physical contents IS the look the record is waiting for. The
        reason rides the ``@dangerous`` audit trail, never the ledger record.
        """
        del reason  # consumed by @dangerous audit; facade ignores body
        instance = await self._require(labware_id)
        self._refuse_non_rack(instance)
        if isinstance(instance, TipRackInstance):
            known = {s.identifier for s in instance.tip_rack.tip_spots()}
            unknown = [p for p in tip_positions_present if known and p not in known]
            if unknown:
                raise ValueError(f"rack '{labware_id}' has no positions {unknown}")
        await self._contents.assert_tips(instance.ref, list(tip_positions_present))
        await self._push_well_state_if_on_deck(instance)

    @dangerous(
        name="labware.mark_tips_used",
        level=DangerLevel.PHYSICAL,
        message="Mark tips {positions} on rack '{labware_id}' as no longer "
                "present. You are asserting those positions are physically "
                "empty; every other position keeps its tracked state, and the "
                "rack's next pick moves past them.",
        requires_reason=True,
    )
    async def mark_tips_used(
        self, labware_id: str, positions: list[str], *,
        reason: str | None = None,
    ) -> list[str]:
        """Operator-asserted subtraction: these positions are empty now.

        The repair for the ordinary case -- a pick found air, or a hand took a
        column -- without retyping the ninety positions that did not change.
        Returns what the rack still holds.
        """
        del reason  # consumed by @dangerous audit; facade ignores body
        instance = await self._require(labware_id)
        self._refuse_non_rack(instance)
        remaining = await self._contents.mark_tips_used(instance.ref, list(positions))
        await self._push_well_state_if_on_deck(instance)
        return remaining

    @dangerous(
        name="labware.confirm_tip_state",
        level=DangerLevel.OPERATOR,
        message="Confirm that rack '{labware_id}' physically matches its "
                "tracked tip layout. The layout becomes an operator-asserted "
                "ledger baseline and the rack stops reading stale.",
    )
    async def confirm_tip_state(
        self, labware_id: str,
        *, reason: str | None = None,
    ) -> None:
        """Operator agreement that the tracked layout matches the rack.

        Appends the current projection as a SET_TIP_STATE baseline (a durable
        receipt of what was agreed) and clears the stale mark. Refused
        when nothing is tracked: confirming then would assert an empty rack
        nobody stated -- use ``set_tip_state`` to declare the layout instead.
        """
        del reason  # consumed by @dangerous audit; facade ignores body
        instance = await self._require(labware_id)
        self._refuse_non_rack(instance)
        await self._refuse_a_confirm_the_record_cannot_back(instance, "tip-state")
        positions = await self._projected_tip_positions(instance)
        if positions is None:
            raise NothingToConfirmError(
                f"nothing tracked on rack '{labware_id}' to confirm; "
                f"assert the layout with set_tip_state instead",
                settle_with=set_verb("tip-state"),
            )
        await self._contents.assert_tips(instance.ref, positions)

    async def _projected_tip_positions(self, instance: LabwareInstance) -> list[str] | None:
        """What ``get_tip_state`` shows, or None when the record knows nothing.

        Deliberately the same fold as the read surface: confirm means "the rack
        matches what the system showed me", so it must never record a layout the
        read surface never displayed. A rack the record knows nothing about is
        asserted with ``set_tip_state``, not confirmed.
        """
        contents = await self._contents.of(instance.ref)
        if not contents.is_known:
            return None
        return contents.tip_positions_present

    def _refuse_non_rack(self, instance: LabwareInstance) -> None:
        """Tip verbs on a plate are an id mix-up, not a lenient no-op. A row
        whose template this system does not declare stays allowed: the operator
        can still assert or read state on it."""
        template = self._template_for(instance)
        if template is not None and not isinstance(template, TipRackTemplate):
            raise ValueError(f"labware '{instance.name}' is not a tip rack")

    def _template_for(self, instance: LabwareInstance) -> LabwareTemplate | None:
        try:
            return self._system.get_labware_template(instance.template_name)
        except KeyError:
            return None

    async def _push_well_state_if_on_deck(self, instance: LabwareInstance) -> None:
        """Re-seed the driver tracker for this one labware so it matches the
        ledger after a set. No-op when the labware is unplaced or not on an LH
        deck: the ledger holds the value and the next placement seeds it.

        Only this labware is re-seeded. Correcting the tips on one rack is not a
        reason to destroy and re-create every plate beside it, which is what a
        deck-wide rebuild does, on a live instrument, with no way back if it
        stops half way."""
        location = self._location_service.present_location(instance)
        if location is None:
            return
        await self._system.labware_placer.contents_set(instance, location)

    # -- Operator clear surfaces ---------------------------------------------

    @dangerous(
        name="labware.clear_submission",
        level=DangerLevel.CRITICAL,
        message="Clear the labware of submission '{submission_id}' from the "
                "record. Deck residents declared leave-in-place are kept.",
    )
    async def clear_submission_labware(
        self, submission_id: str, *, force: bool = False, reason: str | None = None,
    ) -> ClearSubmissionResult:
        """Walk every thread tied to ``submission_id`` and clear its
        labware unless the thread template declared ``end_leave_in_place``.

        Finished runs are the normal case: the operator clears the platform
        once the plates have stopped moving, so the walk covers terminal
        executions as well as live ones.

        ``force=True`` bypasses the refusal that fires when the execution
        this submission belongs to is still running, or when another running
        execution holds or names one of its plates. Force does NOT bypass the
        reuse-bound skip: deck-resident reagents always survive a
        per-submission clear, regardless of force.

        Reuse-bound (deck-resident) labware is SKIPPED so operators don't
        accidentally wipe shared reagents when clearing one submission;
        the skipped instance ids are surfaced on the result so the
        operator sees what survived.

        A submission the runtime has never heard of raises ``KeyError``.
        Answering it with an empty clear tells an operator holding a stale
        or mistyped id that the deck is already clear.
        """
        candidates: list[LabwareInstance] = []
        preserved_ids: list[str] = []
        owning_active = False
        known = False
        for execution in self._iter_all_executions():
            owns_submission = any(
                s.id == submission_id for s in execution.submissions
            )
            ew = execution.executing_workflow
            threads = () if ew is None else ew.threads
            for thread in threads:
                ti = thread.thread_instance
                if ti.submission_id != submission_id:
                    continue
                owns_submission = True
                tt = ti.thread_template
                if tt is not None and tt.end_leave_in_place:
                    if ti.labware is not None:
                        preserved_ids.append(ti.labware.id)
                    continue  # reuse-bound; survives the clear
                if ti.labware is not None:
                    candidates.append(ti.labware)
            if owns_submission:
                known = True
                if not execution.task.done():
                    owning_active = True
        if not known:
            raise KeyError(f"No submission with id '{submission_id}'")
        if owning_active and not force:
            raise ActiveExecutionRefusedError(
                scope="submission", submission_id=submission_id,
            )
        in_use = next(
            (i for i in candidates if self._has_active_thread_for(i) or self._named_by_a_submission_with_no_thread_yet(i)),
            None,
        )
        if in_use is not None and not force:
            raise ActiveExecutionRefusedError(scope="labware", labware_id=in_use.id)
        cleared: list[str] = []
        for instance in candidates:
            cleared.append(instance.id)
            await self._wipe_labware(instance)
        return ClearSubmissionResult(
            cleared=cleared, preserved_reuse_bound=preserved_ids,
        )

    @dangerous(
        name="labware.release_mover_hold",
        level=DangerLevel.PHYSICAL,
        message="Release the labware the record says '{mover_name}' is holding. "
                "No mover is driven; you are stating what is really in the jaws. "
                "With a location, the labware is asserted to be there and the "
                "thread carrying it plans a fresh move from it. With none, the "
                "labware is discharged: say that only when it is off the deck.",
        requires_reason=True,
    )
    async def release_mover_hold(
        self, mover_name: str, to_location: str | None = None,
        *, force: bool = False, reason: str | None = None,
    ) -> MoverHoldRelease:
        """Free one mover the record says is holding a labware.

        An abort taken while a plate is genuinely in the jaws leaves a real
        hold, and a mover that holds one refuses every later pick. The clear
        existed only as ``reset_labware_state``, reachable through the
        clear-all panic button, which wipes every other labware in the
        runtime as well.

        The mover is what the refusal names, so the mover is what this takes.
        ``to_location`` is where the operator has actually put the labware;
        without one the labware is discharged, which is the answer when the
        jaws are empty and the record is wrong. Discharging refuses while a
        live thread still carries the labware unless ``force``, the same rule
        ``discharge_labware`` applies. Asserting a position refuses only what
        ``edit_location`` refuses; a carrying thread is told to plan again.
        """
        del reason  # consumed by @dangerous audit; facade ignores body
        mover = self._find_mover(mover_name)
        held = mover.labware
        if held is None:
            raise MoverHoldsNothingError(mover_name)
        if to_location is not None:
            self._refuse_a_gripper_as_the_answer(to_location)
            await self._assert_position(held.id, to_location)
            # Empty jaws must not still name where the plate was picked from:
            # the labware is gone from the gripper, and the pick origin is the
            # other half of the same answer.
            await self._system.labware_placer.jaws_released(mover)
            return MoverHoldRelease(
                mover_name=mover_name, labware_id=held.id,
                labware_name=held.name, released_to=to_location, discharged=False,
            )
        if not force and self._has_active_thread_for(held):
            raise ActiveExecutionRefusedError(scope="labware", labware_id=held.id)
        # The wipe walks the grippers too (see ``_all_locations``), so this is
        # what frees the jaws; the reset drops the pick origin with them.
        await self._wipe_labware(held)
        await self._system.labware_placer.jaws_released(mover)
        return MoverHoldRelease(
            mover_name=mover_name, labware_id=held.id,
            labware_name=held.name, released_to=None, discharged=True,
        )

    def _refuse_a_gripper_as_the_answer(self, to_location: str) -> None:
        """A gripper is not somewhere an operator can have put the plate.

        Placing onto the position it already occupies is a no-op, so the hold
        would survive a call that reported success.
        """
        if self._system.system_map.find_gripper_location(to_location) is None:
            return
        raise ValueError(
            f"{to_location!r} is a mover's jaws, not a place a plate can be "
            f"put down. Name the position you actually put it at, or leave "
            f"the location out to discharge the labware."
        )

    def _find_mover(self, mover_name: str) -> TransporterBase:
        for mover in self._system.movers:
            if mover.name == mover_name:
                return mover
        known = sorted(mover.name for mover in self._system.movers)
        raise KeyError(
            f"No mover named {mover_name!r}. Movers in this system: {known!r}."
        )

    @dangerous(
        name="labware.discharge",
        level=DangerLevel.OPERATOR,
        message="Discharge labware '{labware_id}': it leaves its position and "
                "the live registry. Its record stays readable.",
    )
    async def discharge_labware(
        self, labware_id: str, *, force: bool = False, reason: str | None = None,
    ) -> None:
        """Remove ONE labware instance from every runtime store.

        ``force=True`` bypasses the refusal that fires when any active
        thread (in any non-terminal execution) currently references this
        labware, or a running execution has named it and not built its thread yet. Use after physically picking the labware off the deck;
        force only when you've verified no in-flight thread still holds it.
        Releasing an ``AWAITING_MANUAL_REMOVE`` park needs no force: the
        thread waiting on that removal is exempt from the refusal.
        """
        instance = await self._find_by_id(labware_id)
        if instance is None:
            raise LabwareNotFoundError(labware_id)
        if not force and (
            self._has_active_thread_for(instance) or self._named_by_a_submission_with_no_thread_yet(instance)
        ):
            raise ActiveExecutionRefusedError(
                scope="labware", labware_id=labware_id,
            )
        await self._wipe_labware(instance)

    @dangerous(
        name="labware.clear_all",
        level=DangerLevel.CRITICAL,
        message="Clear every labware from the record, deck residents included, "
                "and reset every device's labware projection.",
    )
    async def clear_all_labware(
        self, *, force: bool = False, reason: str | None = None,
    ) -> list[str]:
        """Panic button: clear every labware in the runtime.

        ``force=True`` bypasses the refusal that fires when any execution
        is non-terminal. There is no per-labware skip on this surface --
        clear-all wipes reuse-bound labware too (unlike
        ``clear_submission_labware``). Reserve for "the runtime is corrupt
        past selective recovery" scenarios; routine recovery should use
        ``discharge_labware`` (one) or ``clear_submission_labware`` (one
        submission).
        """
        if not force and self._has_any_active_thread():
            raise ActiveExecutionRefusedError(scope="all")
        cleared: list[str] = []
        # Snapshot first; the list mutates as we remove entries.
        for instance in list(self._system.labwares):
            cleared.append(instance.id)
            await self._wipe_labware(instance)
        holders: list[ILabwareStateHolder] = [*self._system.devices, *self._system.movers]
        saved_only = await self._system.labware_placer.cleared_all(self._all_locations(), holders)
        return cleared + [labware_id for labware_id in saved_only if labware_id not in cleared]

    def _all_locations(self) -> Iterator[Location]:
        """Every Location a labware can be at. Deck sites are covered by
        ``system.locations`` (flat model: they are top-level graph nodes); the
        movers' grippers are not on the graph at all, and without them a
        discharge leaves the arm still holding the plate it just wrote off."""
        for location in self._system.locations:
            yield location
        for mover in self._system.movers:
            yield mover.gripper_location

    async def _wipe_labware(self, instance: LabwareInstance) -> None:
        """Remove a single instance from every runtime store.

        The placer's ``left`` frees each slot that still holds it, retires its
        position, clears its saved position and takes it off that site's deck.
        The engine registry drops it. The labware service keeps the row:
        discharge is a lifecycle end, not a retraction, so history and volumes
        stay queryable. A save that fails reaches the caller once the decks and
        the registry are cleaned.

        The driver decks are then cleared again, without asking any store where
        the labware was. A thread that ends retires its labware and frees the
        slot, so a plate it left standing on a liquid handler would ride through
        its own discharge and refuse the next run that slot.

        A device that cannot be reached never stops a discharge: it is what an
        operator reaches for WHEN a handler is down.
        """
        carrier = self._active_thread_for(instance)
        holders = [location for location in self._all_locations() if location.labware is instance]
        for location in holders:
            try:
                await self._system.labware_placer.left(instance, location)
            except PositionNotSavedError:
                # The discharge below clears the same saved position again, and raises if that fails too.
                pass
            except Exception as exc:
                orca_logger.warning(
                    "could not take %s off the deck at %s (%s); that deck may still hold it.",
                    instance.name, location.position_id, exc,
                )
        # Retired whatever a holder or a device said above, so the record never keeps a plate the operator removed.
        # A labware that never arrived was only expected; a wipe leaves neither behind, on no deck in any world.
        try:
            await self._system.labware_placer.left(instance, None)
        finally:
            self._system.remove_labware(instance.id)
        if carrier is not None:
            carrier.notify_labware_discharged()

    def tell_threads_to_look_again(self) -> None:
        for execution in self._iter_active_executions():
            ew = execution.executing_workflow
            if ew is None:
                continue
            for thread in ew.threads:
                thread.notify_labware_moved()

    def _active_thread_for(
        self, instance: LabwareInstance,
    ) -> ExecutingLabwareThread | None:
        """The live thread carrying this labware, or None.

        What counts as carrying is ``holds_labware``.
        """
        for execution in self._iter_active_executions():
            ew = execution.executing_workflow
            if ew is None:
                continue
            for thread in ew.threads:
                if thread.thread_instance.labware is not instance:
                    continue
                if holds_labware(_status_of(thread)):
                    return thread
        return None

    def _has_active_thread_for(self, instance: LabwareInstance) -> bool:
        return self._active_thread_for(instance) is not None

    def _named_by_a_submission_with_no_thread_yet(self, instance: LabwareInstance) -> bool:
        """A submission claims its named labware from acceptance; its threads are built later. Once a thread holds
        the labware, `_has_active_thread_for` judges it instead."""
        for execution in self._iter_active_executions():
            ew = execution.executing_workflow
            if any(t.thread_instance.labware is instance for t in (() if ew is None else ew.threads)):
                continue
            if any(
                claim.labware_instance is not None and claim.labware_instance.id == instance.id
                for submission in execution.submissions
                for claim in submission.resolved_acquisitions.values()
            ):
                return True
        return False

    def _has_any_active_thread(self) -> bool:
        for _ in self._iter_active_executions():
            return True
        return False

    def _iter_active_executions(self) -> Iterator[Execution]:
        """Snapshot active executions before iteration.

        Callers iterate while doing async work
        (events, store writes) so a live generator over the underlying
        ``iter_executions()`` is fragile against mid-iteration mutation
        of the registry. Materialize the snapshot up front; iterating
        the resulting tuple is allocation-cheap and removes one
        possible wedge source during ``labware_discharge`` writes that
        race with execution start/finish.
        """
        return iter(tuple(
            e for e in self._iter_all_executions() if not e.task.done()
        ))

    def _iter_all_executions(self) -> Iterator[Execution]:
        """Snapshot every tracked execution, finished ones included."""
        return iter(tuple(self._execution_iterator.iter_executions()))

    # -- Internals -----------------------------------------------------------

    async def _snapshot(self, instance: LabwareInstance) -> LabwareSnapshot:
        try:
            loc = self._location_service.get(instance)
            current_location: str | None = loc.name
            placement: PlacementState | None = self._location_service.placement(instance)
        except KeyError:
            # Registered but never placed anywhere: no position, no placement.
            current_location = None
            placement = None
        # `instance._template` is only populated for labware spawned through the
        # in-process template factory; rehydrated instances carry only the
        # ``template_name`` string. Read the string directly so DB-rehydrated
        # rows surface the original template name instead of "".
        return LabwareSnapshot(
            id=instance.id,
            name=instance.name,
            template_name=instance.template_name,
            labware_type=instance.labware_type,
            barcode=instance.barcode,
            current_location=current_location,
            placement=placement,
            carry_override=instance.carry_override,
            contents_provenance=(await self._contents.of(instance.ref)).provenance,
        )
