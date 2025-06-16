from abc import ABC, abstractmethod
import asyncio
import uuid
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.transporter_resource import TransporterEquipment
from orca.events.execution_context import MoveActionExecutionContext, ThreadExecutionContext
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.status_enums import ActionStatus



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
    def transporter(self) -> TransporterEquipment:
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

class MoveAction(IMoveAction):
    def __init__(self,
                 labware: LabwareInstance,
                 source: Location,
                 target: Location,
                 transporter: TransporterEquipment):
        self._id = str(uuid.uuid4())
        self._labware = labware
        self._source = source
        self._target = target
        self._transporter = transporter
        self._release_reservation_on_place = True
        self._reservation = LocationReservation(self.target, self.labware)

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
    def transporter(self) -> TransporterEquipment:
        return self._transporter

    @property
    def  release_reservation_on_place(self) -> bool:
        return self._release_reservation_on_place

    @property
    def reservation(self) -> LocationReservation:
        return self._reservation

    def set_reservation(self, reservation: LocationReservation) -> None:
        self._reservation = reservation

    def set_release_reservation_on_place(self, release: bool) -> None:
        self._release_reservation_on_place = release


class ExecutingMoveAction(IMoveAction):
    def __init__(self,
                 status_manager: StatusManager,
                 context: ThreadExecutionContext,
                 action: IMoveAction) -> None:
        super().__init__()
        self._status_manager = status_manager
        self._action = action
        self._context = context
        self.status = ActionStatus.CREATED
        self.status = ActionStatus.AWAITING_MOVE_RESERVATION
        self._is_executing = asyncio.Lock()

    @property
    def status(self) -> ActionStatus:
        status = self._status_manager.get_status(self._action.id)
        return ActionStatus[status]

    @status.setter
    def status(self, status: ActionStatus) -> None:
        id = self._action.id
        context = MoveActionExecutionContext(
                                 self._context.workflow_id,
                                 self._context.workflow_name,
                                 self._context.thread_id,
                                    self._context.thread_name,
                                    id,
                                    status.name.upper(),
                             )
        self._status_manager.set_status("ACTION", id, status.name, context)

    async def _execute_action(self) -> None:
        if self._action.reservation is None:
            raise ValueError("Reservation must be set before performing action")
        if self._action.labware is None:
            raise ValueError("Labware must be set before performing action")
        if self._action.target.labware is not None:
            raise ValueError("Target location is occupied")

        self.status = ActionStatus.PREPARING_TO_MOVE
        # move the labware
        await self._action.source.prepare_for_pick(self._action.labware)
        await self._action.target.prepare_for_place(self._action.labware)

        self.status = ActionStatus.PICKING
        await self._action.transporter.pick(self._action.source)
        await self._action.source.notify_picked(self._action.labware)

        self.status = ActionStatus.PLACING
        await self._action.transporter.place(self._action.target)
        await self._action.target.notify_placed(self._action.labware)

        # await notify_picked
        if self._action.release_reservation_on_place:
            # TODO: This should be handled elsewhere and the reservation manager shouldn't be within this class
            self._action.reservation.release_reservation()

    async def execute(self) -> None:
        async with self._is_executing:
            if self.status == ActionStatus.COMPLETED:
                return
            if self.status == ActionStatus.ERRORED:
                raise ValueError("Action has errored, cannot execute")
            try:
                await self._execute_action()
            except Exception as e:
                self.status = ActionStatus.ERRORED
                raise e
            self.status = ActionStatus.COMPLETED

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
    def transporter(self) -> TransporterEquipment:
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