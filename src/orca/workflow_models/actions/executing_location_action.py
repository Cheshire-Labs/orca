from orca.events.execution_context import LocationActionExecutionContext, MethodExecutionContext
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.action_template import Device
from orca.workflow_models.actions.location_action import LocationAction
from orca.workflow_models.actions.location_action_interface import ILocationAction
from orca.workflow_models.status_enums import ActionStatus
from orca.workflow_models.status_manager import StatusManager


import asyncio
from typing import List


class ExecutingLocationAction(ILocationAction):
    def __init__(self,
                 status_manager: StatusManager,
                 action: LocationAction,
                 context: MethodExecutionContext,
                 ) -> None:
        super().__init__()
        self._status_manager = status_manager
        self._context = context
        self._action = action
        self.status = ActionStatus.CREATED
        self._is_executing = asyncio.Lock()

    @property
    def status(self) -> ActionStatus:
        status = self._status_manager.get_status(self._action.id)
        return ActionStatus[status]

    @status.setter
    def status(self, status: ActionStatus) -> None:
        id = self._action.id
        context = LocationActionExecutionContext(
            self._context.workflow_id,
            self._context.workflow_name,
            self._context.method_id,
            self._context.method_name,
            id,
            status.name.upper(),
        )
        self._status_manager.set_status("ACTION", id, status.name, context)

    async def _execute_action(self) -> None:
        self._status = ActionStatus.AWAITING_CO_THREADS
        await self.all_labware_is_present.wait()
        self._status = ActionStatus.EXECUTING_ACTION
        self._ensure_all_labware_present()

        await self._action.execute()


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

    def _ensure_all_labware_present(self) -> None:
        missing_labware = self._action.get_missing_input_labware()
        if len(missing_labware) > 0:
            raise ValueError(
                f"Missing labware for action '{self._action.command}' (ID: {self._action.id}) at location '{self._action.location}': "
                f"{', '.join([labware.name for labware in missing_labware])}"
            )
    @property
    def id(self) -> str:
        return self._action.id

    @property
    def location(self) -> Location:
        return self._action.location

    @property
    def resource(self) -> Device:
        return self._action.resource

    @property
    def expected_inputs(self) -> List[LabwareInstance]:
        return self._action.expected_inputs

    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        return self._action.expected_outputs

    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance):
        return self._action.assign_input(template_slot, input)

    @property
    def reservation(self) -> LocationReservation:
        return self._action.reservation

    def release_reservation(self) -> None:
        return self._action.release_reservation()

    def get_missing_input_labware(self) -> List[LabwareInstance]:
        return self._action.get_missing_input_labware()

    def get_present_output_labware(self) -> List[LabwareInstance]:
        return self._action.get_present_output_labware()

    def all_output_labware_removed(self) -> bool:
        return self._action.all_output_labware_removed()

    @property
    def all_labware_is_present(self) -> asyncio.Event:
        return self._action.all_labware_is_present