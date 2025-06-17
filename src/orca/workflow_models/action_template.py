from orca.resource_models.base_resource import Equipment
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.resource_pool import EquipmentResourcePool


from typing import Any, Dict, List, Optional, Union


class ActionTemplate:
    """ Creates a template for an action.  An action is a single operation that can be performed on a set of labware using a resource or resource pool."""
    def __init__(self, 
                 resource: Equipment | EquipmentResourcePool,
                 command: str,
                 inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 outputs: Optional[List[Union[LabwareTemplate, AnyLabwareTemplate]]] = None,
                 options: Optional[Dict[str, Any]] = None):
        """ Initializes an ActionTemplate instance.
        Args:
            resource (Equipment | EquipmentResourcePool): The resource or resource pool that will be used for the action.
            command (str): The command to be executed by the resource.
            inputs (List[Union[LabwareTemplate, AnyLabwareTemplate]]): A list of labware templates that will be used as inputs for the action.
            outputs (Optional[List[Union[LabwareTemplate, AnyLabwareTemplate]]], optional): A list of labware templates that will be used as outputs for the action. Defaults to None.
            options (Optional[Dict[str, Any]], optional): A dictionary of options to configure the action. Defaults to None."""
        if isinstance(resource, Equipment):
            self._resource_pool: EquipmentResourcePool = EquipmentResourcePool(resource.name, [resource])
        else:
            self._resource_pool = resource
        self._command = command
        self._options: Dict[str, Any] = {} if options is None else options
        self._inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = inputs
        # outputs default to be the same as the inputs unless specified
        self._outputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = outputs if outputs is not None else inputs

    @property
    def resource_pool(self) -> EquipmentResourcePool:
        return self._resource_pool

    @property
    def inputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._inputs

    @property
    def outputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._outputs

    @property
    def command(self) -> str:
        return self._command

    @property
    def options(self) -> Dict[str, Any]:
        return self._options