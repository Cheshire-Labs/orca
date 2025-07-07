from typing import Any, Dict, List, Optional

from pyparsing import ABC, abstractmethod
from orca.driver_management.driver_interfaces import ICentrifuge, IDelidder, IGenericExecutable, IProtocolRunner, IReader, ISealer, IShaker
from orca.resource_models.devices import Device
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.workflow_models.actions.location_action import CentrifugeLocationAction, DelidLocationAction, ExecuteCommandAction, ExecuteMethodAction, ExecuteMethodType, LocationAction, ReadLocationAction, RunProtocolAction, SealLocationAction, ShakeLocationAction


class ActionTemplate(ABC):
    """ Creates a template for an action.  An action is a single operation that can be performed on a set of labware using a resource or resource pool."""
    def __init__(self, 
                 operation_name: str,
                 resource: Device | ResourcePool,
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: Optional[List[LabwareTemplate | AnyLabwareTemplate]] = None,
                 options: Optional[Dict[str, Any]] = None) -> None:
        """ Initializes an ActionTemplate instance.
        Args:
            operation_name (str): The name of the operation that this action performs.
            resource (Device | EquipmentResourcePool): The resource or resource pool that this action uses.
            inputs (List[LabwareTemplate | AnyLabwareTemplate]): The input labware templates for this action.
            outputs (Optional[List[LabwareTemplate | AnyLabwareTemplate]]): The output labware templates for this action. Defaults to the same as inputs.
            options (Optional[Dict[str, Any]]): Additional options for the action.
        """
        self._resource_pool: ResourcePool
        if isinstance(resource, Device):
            self._resource_pool = ResourcePool(resource.name, [resource])
        elif isinstance(resource, ResourcePool):
            self._resource_pool = resource
        else:
            raise TypeError("resource must be an Equipment or EquipmentResourcePool")
        self._operation_name = operation_name
        self._options: Dict[str, Any] = {} if options is None else options
        self._inputs = inputs
        # outputs default to be the same as the inputs unless specified
        self._outputs = outputs if outputs is not None else inputs

    @property
    def resource_pool(self) -> ResourcePool:
        return self._resource_pool

    @property
    def inputs(self) -> List[LabwareTemplate | AnyLabwareTemplate]:
        return self._inputs

    @property
    def outputs(self) -> List[LabwareTemplate | AnyLabwareTemplate]:
        return self._outputs

    @property
    def operation_name(self) -> str:
        """Returns the name of the operation that this action performs."""
        return self._operation_name
    
    @property
    def options(self) -> Dict[str, Any]:
        """Returns the options for this action."""
        return self._options
    
    @abstractmethod
    def get_location_action(self) -> LocationAction:
        """Returns a LocationAction instance with the assigned labware manager."""
        raise NotImplementedError("Subclasses must implement the get_location_action method.")
    

class ExecuteCommand(ActionTemplate):
    """ Sends a command string and options dictionary to the resource to execute a command once the labware reaches the resource."""
    def __init__(self,
                 resource: Device | ResourcePool,
                 command: str,
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: List[LabwareTemplate | AnyLabwareTemplate],
                 options: Dict[str, Any] | None = None
                 ):
        """ Sends a command string and options dictionary to the resource to execute a command once the labware reaches the resource.
        Args:
            resource (Device | ResourcePool): The resource or resource pool that this action uses.
            command (str): The command to execute on the resource.
            inputs (List[LabwareTemplate | AnyLabwareTemplate]): The input labware templates for this action.
            outputs (List[LabwareTemplate | AnyLabwareTemplate]): The output labware templates for this action.
            options (Dict[str, Any] | None): Additional options for the action.
        """
        self._command = command
        self._validate_resource(resource)
        super().__init__("execute", resource, inputs, outputs, options)

    def get_location_action(self) -> LocationAction:
        location_action = ExecuteCommandAction(
            command=self._command,
            options=self.options
        )
        return location_action
        
    def _validate_resource(self, resource: Device | ResourcePool) -> None:
        if isinstance(resource, ResourcePool):
            if not all(isinstance(r, IGenericExecutable) for r in resource.resources):
                raise TypeError("All devices in the resource pool must implement IGenericExecutable.")
        elif not isinstance(resource, IGenericExecutable):
            raise TypeError("Resource must implement IGenericExecutable.")
        
        
class PythonMethod(ActionTemplate):
    """ Executes a Python method once the labware reaches the resource. The method must accept the resource, inputs, outputs, and options."""
    def __init__(self,
                 resource: Device | ResourcePool,
                 method: ExecuteMethodType,
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: List[LabwareTemplate | AnyLabwareTemplate],
                 options: Dict[str, Any] | None = None
                 ):
        """ Executes a Python method once the labware reaches the resource.
        Args:
            resource (Device | ResourcePool): The resource or resource pool that this action uses.
            method (ExecuteMethodType): The Python method to execute.  Must accept the resource, inputs, outputs, and options.
            inputs (List[LabwareTemplate | AnyLabwareTemplate]): The input labware templates for this action.
            outputs (List[LabwareTemplate | AnyLabwareTemplate]): The output labware templates for this action.
            options (Dict[str, Any] | None): Additional options for the action.
        """
        self.method = method
        super().__init__("python_method", resource, inputs, outputs, options)

    def get_location_action(self) -> LocationAction:
        location_action = ExecuteMethodAction(
            "python_method",
            self.method,
            self.options
        )
        return location_action



class RunProtocol(ActionTemplate):
    """ Runs a protocol file with the given parameters once the labware reaches the resource."""
    def __init__(self,
                 resource: Device | ResourcePool,
                 protocol_filepath: str,
                 parameters: Dict[str, Any],
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: List[LabwareTemplate | AnyLabwareTemplate],
                 options: Dict[str, Any] | None = None
                 ):
        """ Runs a protocol file with the given parameters once the labware reaches the resource.
        Args:
            resource (Device | ResourcePool): The resource or resource pool that this action uses.
            protocol_filepath (str): The file path to the protocol file to run.
            parameters (Dict[str, Any]): The parameters to pass to the protocol.
            inputs (List[LabwareTemplate | AnyLabwareTemplate]): The input labware templates for this action.
            outputs (List[LabwareTemplate | AnyLabwareTemplate]): The output labware templates for this action.
            options (Dict[str, Any] | None): Additional options for the action.
        """
        self.protocol_filepath = protocol_filepath
        self.parameters = parameters
        self._validate_resource(resource)
        super().__init__("run_protocol", resource, inputs, outputs, options)

    def get_location_action(self) -> LocationAction:
        location_action = RunProtocolAction(
            "run_protocol",
            self.protocol_filepath,
            self.parameters,
            self.options
        )
        return location_action

    def _validate_resource(self, resource: Device | ResourcePool) -> None:
        if isinstance(resource, ResourcePool):
            if not all(isinstance(r, IProtocolRunner) for r in resource.resources):
                raise TypeError("All devices in the resource pool must implement IProtocolRunner.")
        elif not isinstance(resource, IProtocolRunner):
            raise TypeError("Resource must implement IProtocolRunner.")


class Seal(ActionTemplate):
    """ Seals the labware at a specified temperature and duration once the labware reaches the resource."""
    def __init__(self,
                 resource: Device | ResourcePool,
                 temperature: int,
                 duration: float,
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: List[LabwareTemplate | AnyLabwareTemplate],
                 options: Dict[str, Any] | None = None
                 ):
        """ Seals the labware at a specified temperature and duration once the labware reaches the resource.
        Args:
            resource (Device | ResourcePool): The resource or resource pool that this action uses.
            temperature (int): The temperature at which to seal the labware.
            duration (float): The duration for which to seal the labware.
            inputs (List[LabwareTemplate | AnyLabwareTemplate]): The input labware templates for this action.
            outputs (List[LabwareTemplate | AnyLabwareTemplate]): The output labware templates for this action.
            options (Dict[str, Any] | None): Additional options for the action.
        """
        self.temperature = temperature
        self.duration = duration
        self._validate_resource(resource)
        super().__init__("seal", resource, inputs, outputs, options)

    def get_location_action(self) -> LocationAction:
        location_action = SealLocationAction(
            "seal",
            self.temperature,
           self.duration,)
        return location_action

    def _validate_resource(self, resource: Device | ResourcePool) -> None:
        if isinstance(resource, ResourcePool):
            if not all(isinstance(r, ISealer) for r in resource.resources):
                raise TypeError("All devices in the resource pool must implement ISealer.")
        elif not isinstance(resource, ISealer):
            raise TypeError("Resource must implement ISealer.")

class Shake(ActionTemplate):
    """ Shakes the labware at a specified speed and duration once the labware reaches the resource."""
    def __init__(self,
                 resource: Device | ResourcePool,
                 duration: int,
                 speed: int,
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: List[LabwareTemplate | AnyLabwareTemplate],
                 options: Dict[str, Any] | None = None
                 ):
        """ Shakes the labware at a specified speed and duration once the labware reaches the resource.
        Args:
            resource (Device | ResourcePool): The resource or resource pool that this action uses.
            duration (int): The duration for which to shake the labware.
            speed (int): The speed at which to shake the labware.
            inputs (List[LabwareTemplate | AnyLabwareTemplate]): The input labware templates for this action.
            outputs (List[LabwareTemplate | AnyLabwareTemplate]): The output labware templates for this action.
            options (Dict[str, Any] | None): Additional options for the action.
        """
        self.duration = duration
        self.speed = speed
        self._validate_resource(resource)
        super().__init__("shake", resource, inputs, outputs, options)

    def get_location_action(self) -> LocationAction:
        location_action = ShakeLocationAction(
            "shake",
            self.speed,
            self.duration
        )
        return location_action

    def _validate_resource(self, resource: Device | ResourcePool) -> None:
        if isinstance(resource, ResourcePool):
            if not all(isinstance(r, IShaker) for r in resource.resources):
                raise TypeError("All devices in the resource pool must implement IShaker.")
        elif not isinstance(resource, IShaker):
            raise TypeError("Resource must implement IGenericEIShakerxecutable.")

class Centrifuge(ActionTemplate):
    """ Centrifuges the labware at a specified speed and duration once the labware reaches the resource."""
    def __init__(self,
                 resource: Device | ResourcePool,
                 speed: int,
                 duration: int,
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: List[LabwareTemplate | AnyLabwareTemplate],
                 options: Dict[str, Any] | None = None
                 ):
        """ Centrifuges the labware at a specified speed and duration once the labware reaches the resource.
        Args:
            resource (Device | ResourcePool): The resource or resource pool that this action uses.
            speed (int): The speed at which to centrifuge the labware.
            duration (int): The duration for which to centrifuge the labware.
            inputs (List[LabwareTemplate | AnyLabwareTemplate]): The input labware templates for this action.
            outputs (List[LabwareTemplate | AnyLabwareTemplate]): The output labware templates for this action.
            options (Dict[str, Any] | None): Additional options for the action.
        """
        self.speed = speed
        self.duration = duration
        self._validate_resource(resource)
        super().__init__("centrifuge", resource, inputs, outputs, options)

    def get_location_action(self) -> LocationAction:
        location_action = CentrifugeLocationAction(
            "centrifuge",
            self.speed,
            self.duration,
        )
        return location_action

    def _validate_resource(self, resource: Device | ResourcePool) -> None:
        if isinstance(resource, ResourcePool):
            if not all(isinstance(r, ICentrifuge) for r in resource.resources):
                raise TypeError("All devices in the resource pool must implement ICentrifuge.")
        elif not isinstance(resource, ICentrifuge):
            raise TypeError("Resource must implement ICentrifuge.")
        

class Read(ActionTemplate):
    """ Performs a plate read at reader device using a protocol file and writes the output data to a file."""
    def __init__(self,
                 resource: Device | ResourcePool,
                 protocol_filepath: str,
                 output_filepath: str,
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: List[LabwareTemplate | AnyLabwareTemplate],
                 options: Dict[str, Any] | None = None
                 ):
        """ Performs a plate read at reader device using a protocol file and writes the output data to a file.
        Args:
            resource (Device | ResourcePool): The resource or resource pool that this action uses.
            protocol_filepath (str): The file path to the protocol file to run.
            output_filepath (str): The file path to write the output data.
            inputs (List[LabwareTemplate | AnyLabwareTemplate]): The input labware templates for this action.
            outputs (List[LabwareTemplate | AnyLabwareTemplate]): The output labware templates for this action.
            options (Dict[str, Any] | None): Additional options for the action.
        """
        self.protocol_filepath = protocol_filepath
        self.output_filepath = output_filepath
        self._validate_resource(resource)
        super().__init__("read", resource, inputs, outputs, options)

    def get_location_action(self) -> LocationAction:
        location_action = ReadLocationAction(
            "read",
            self.protocol_filepath,
            self.output_filepath,
        )
        return location_action

    def _validate_resource(self, resource: Device | ResourcePool) -> None:
        if isinstance(resource, ResourcePool):
            if not all(isinstance(r, IReader) for r in resource.resources):
                raise TypeError("All devices in the resource pool must implement IReader.")
        elif not isinstance(resource, IReader):
            raise TypeError("Resource must implement IReader.")
        
class Delid(ActionTemplate):
    """ Delids the labware once the labware reaches the resource."""
    def __init__(self,
                 resource: Device | ResourcePool,
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: List[LabwareTemplate | AnyLabwareTemplate],
                 options: Dict[str, Any] | None = None
                 ):
        """ Delids the labware once the labware reaches the resource.
        Args:
            resource (Device | ResourcePool): The resource or resource pool that this action uses.
            inputs (List[LabwareTemplate | AnyLabwareTemplate]): The input labware templates for this action.
            outputs (List[LabwareTemplate | AnyLabwareTemplate]): The output labware templates for this action.
            options (Dict[str, Any] | None): Additional options for the action.
        """
        self._validate_resource(resource)
        super().__init__("delid", resource, inputs, outputs, options)

    def get_location_action(self) -> LocationAction:
        location_action = DelidLocationAction("delid")
        return location_action

    def _validate_resource(self, resource: Device | ResourcePool) -> None:
        if isinstance(resource, ResourcePool):
            if not all(isinstance(r, IDelidder) for r in resource.resources):
                raise TypeError("All devices in the resource pool must implement IDelidder.")
        elif not isinstance(resource, IDelidder):
            raise TypeError("Resource must implement IDelidder.")