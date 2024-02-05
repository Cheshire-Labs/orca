import os
from drivers.base_resource import IResource
from drivers.ilabware_transporter import ILabwareTransporter
from drivers.resource_factory import ResourceFactory
from labware import Labware
from location import Location
from method import Action, Method
from system import System
from workflow import LabwareThread, Workflow


from pyaml import yaml


from typing import Any, Dict, List, Optional


class SystemBuilder:
    def __init__(self) -> None:
        self._name: Optional[str] = None
        self._description: Optional[str] = None
        self._version: Optional[str] = None
        self._options: Dict[str, Any] = {}
        self._labware_defs: Dict[str, Dict[str, Any]] = {}
        self._resource_defs: Dict[str, Dict[str, Any]] = {}
        self._location_defs: Dict[str, Dict[str, Any]] = {}
        self._method_defs: Dict[str, Dict[str, Any]] = {}
        self._workflow_defs: Dict[str, Dict[str, Any]] = {}
        self._system: Optional[System] = None
    
    def is_auto_transport(self) -> bool:
        return "auto-transport" in self._options.keys() and self._options["auto-transport"]

    def set_system_name(self, name: str) -> None:
        self._name = name

    def set_system_description(self, description: str) -> None:
        self._description = description

    def set_system_version(self, version: str) -> None:
        self._version = version

    def set_system_options(self, options: Dict[str, Any]) -> None:
        self._options = options

    def add_labware(self, labware_name: str, labware_def: Dict[str, Any]) -> None:
        self._labware_defs[labware_name] = labware_def

    def add_resource(self, resource_name: str, resource_def: Dict[str, Any]) -> None:
        self._resource_defs[resource_name] = resource_def

    def add_location(self, location_name: str, location_def: Dict[str, Any]) -> None:
        self._location_defs[location_name] = location_def

    def add_method(self, method_name: str, method_def: Dict[str, Any]) -> None:
        self._method_defs[method_name] = method_def

    def add_workflow(self, workflow_name: str, workflow_def: Dict[str, Any]) -> None:
        self._workflow_defs[workflow_name] = workflow_def


    def _build_labwares(self, system: System) -> None:
        labwares: Dict[str, Labware] = {}
        for name, labware_def in self._labware_defs.items():
            if "type" not in labware_def.keys():
                raise KeyError(f"Labware {name} does not contain a 'type' definition.  Labware must have a type")
            labwares[name] = Labware(name, labware_def["type"])
        system.labwares = labwares
    
    def _build_resources(self, system: System) -> None:
        resources = {}
        for name, resource_def in self._resource_defs.items():
            if "type" not in resource_def.keys():
                raise KeyError(f"Resource {name} does not contain a 'type' definition.  Resource must have a type")
            resources[name] = ResourceFactory.create(name, resource_def)
        system.resources = resources

    def _build_locations(self, system: System) -> None:
        location_names: List[str] = []
        for _, res in system.resources.items():
            if isinstance(res, ILabwareTransporter):
                accessible_locations = res.get_accessible_locations()
                location_names.extend(accessible_locations)
        self._locations = {name: Location(name) for name in location_names}

    def _build_methods(self, system: System) -> None:

        methods: Dict[str, Method] = {}
        for method_name, method_def in self._method_defs.items():
            method = Method(name=method_name)
            if "actions" not in method_def.keys():
                raise KeyError(f"Method {method_name} does not contain a 'actions' definition.  Method must have actions")
            actions: List[Action] = []
            for action_index, action_def in enumerate(method_def['actions']):
                if "resource" not in action_def.keys():
                    raise KeyError(f"Resource not defined in {method_name} action index {action_index}")
                resource_name = action_def["resource"]

                if resource_name not in system.resources.keys():
                    raise LookupError(f"The resource name '{resource_name}' in method actions is not recognized as a defined resource")
                resource = system.resources[resource_name]


                # get command
                if "command" not in action_def.keys():
                    raise KeyError(f"No 'command' defined in the action (index={action_index}) for {resource_name} in method {method_name}")
                command = action_def["command"]

                # get input labwares
                inputs = None
                if "inputs" in action_def.keys():
                    input_labware_names: List[str] = action_def["inputs"] if isinstance(action_def["inputs"], List) else [action_def["inputs"]]
                    inputs = [system.labwares[labware_name] for labware_name in input_labware_names]

                # get output labwares
                outputs = None
                if "outputs" in action_def.keys():
                    output_labware_names: List[str] = action_def["outputs"] if isinstance(action_def["outputs"], List) else [action_def["outputs"]]
                    outputs = [system.labwares[labware_name] for labware_name in output_labware_names]

                action = Action(resource=resource, command=command, options=action_def, inputs=inputs, outputs=outputs)
                actions.append(action)

            [method.append_action(a) for a in actions]
            methods[method_name] = method
        system.methods = methods
    
    def _build_workflows(self, system: System) -> None:
        workflows: Dict[str, Workflow] = {}
        for workflow_name, workflow_def in self._workflow_defs.items():
            workflow = Workflow(workflow_name)
            if "labwares" not in workflow_def.keys():
                raise KeyError(f"Workflow {workflow_name} does not contain a 'labwares' key, 'labwares' must be defined")
            labwares_list = workflow_def["labwares"]
            for labware_name, labware_thread_def in labwares_list.items():

                # get labware
                if labware_name not in system.labwares.keys():
                    raise LookupError(f"Labware {labware_name} defined in Workflow {workflow_name} not defined.  Labware must be defined.")
                labware = system.labwares[labware_name]

                # get labware start location
                if "start" not in labware_thread_def.keys():
                    raise KeyError(f"Workflow {workflow_name} labware {labware_name} does not contain a 'start' defintion.  Each labware needs a start location")
                start_location_name = labware_thread_def["start"]
                
                # if auto-transport option is on just add the location
                if self.is_auto_transport():
                    system.locations[start_location_name] = Location(start_location_name)
                else:
                    if start_location_name not in system.locations.keys():
                        raise LookupError(f"Location {start_location_name} referenced in workflow {workflow_name} labware {labware_name} is not recognized.  Locations must be defined by the transporting resource.")
                start_location = system.locations[start_location_name]

                # get labware end location
                if "end" not in labware_thread_def.keys():
                    raise KeyError(f"Workflow {workflow_name} labware {labware_name} does not contain an 'end' defintion.  Each labware needs an end location")
                end_location_name = labware_thread_def["end"]
                if self.is_auto_transport():
                    system.locations[start_location_name] = Location(start_location_name)
                else:
                    if end_location_name not in system.locations.keys():
                        raise LookupError(f"Location {end_location_name} referenced in workflow {workflow_name} labware {labware_name} is not recognized.  Locations must be defined by the transporting resource.")
                end_location = system.locations[end_location_name]

                # make labware thread
                labware_thread = LabwareThread(labware=labware, start=start_location, end=end_location)

                # add the methods
                if "steps" not in labware_thread_def.keys():
                    raise KeyError(f"Labware Thread for {labware_name} in workflow {workflow_name} does not define 'steps'.  'steps' must be defined")

                for method_name in labware_thread_def["steps"]:
                    if method_name not in system.methods.keys():
                        raise LookupError(f"Method {method_name} referenced in workflow {workflow_name} is not defined.  Method {method_name} must be defined")
                    method = system.methods[method_name]
                    labware_thread.add_method(method)

                workflow.labware_threads[labware_name] = labware_thread
            workflows[workflow_name] = workflow
        system.workflows = workflows

    def build(self) -> System:
        system = System(self._name, self._description, self._version, self._options)
        self._build_labwares(system)
        self._build_resources(system)
        self._build_locations(system)
        self._build_methods(system)
        self._build_workflows(system)
        return system

class ConfigSystemBuilder:

    def __init__(self) -> None:
        self.system_config_file: Optional[str] = None
        self.labwares_config_file: Optional[str] = None
        self.resources_config_file: Optional[str] = None
        self.locations_config_file: Optional[str] = None
        self.methods_config_file: Optional[str] = None
        self.workflows_config_file: Optional[str] = None
    
    def set_all_config_files(self, config_file: str) -> None:
        self.system_config_file = config_file
        self.labwares_config_file = config_file
        self.resources_config_file = config_file
        self.locations_config_file = config_file
        self.methods_config_file = config_file
        self.workflows_config_file = config_file

    def _build_system(self, builder: SystemBuilder) -> None:
        if self.system_config_file is None:
            raise ValueError("System config file is not set")
        with open(self.system_config_file) as f:
            system_config = yaml.load(f, Loader=yaml.FullLoader)
        if "system" not in system_config.keys():
            raise KeyError("No 'system' defined in config")
        system_config = system_config["system"]
        if "name" in system_config.keys():
            builder.set_system_name(system_config["name"])
        if "description" in system_config.keys():
            builder.set_system_description(system_config["description"])
        if "version" in system_config.keys():
            builder.set_system_version(system_config["version"])
        if "options" in system_config.keys():
            builder.set_system_options(system_config["options"])
        
    def _build_labwares(self, builder: SystemBuilder) -> None:
        if self.labwares_config_file is None:
            raise ValueError("Labwares config file is not set")
        with open(self.labwares_config_file) as f:
            labwares_config = yaml.load(f, Loader=yaml.FullLoader)
        if "labwares" not in labwares_config.keys():
            raise KeyError("No 'labwares' defined in config")
        for labware_name, labware_def in labwares_config["labwares"].items():

            builder.add_labware(labware_name, labware_def)
    
    def _build_resources(self, builder: SystemBuilder) -> None:
        if self.resources_config_file is None:
            raise ValueError("Resources config file is not set")
        with open(self.resources_config_file) as f:
            resources_config = yaml.load(f, Loader=yaml.FullLoader)
        if "resources" not in resources_config.keys():
            raise KeyError("No 'resources' defined in config")
        for resource_name, resource_def in resources_config["resources"].items():

            builder.add_resource(resource_name, resource_def)

    def _build_methods(self, builder: SystemBuilder) -> None:
        if self.methods_config_file is None:
            raise ValueError("Methods config file is not set")
        with open(self.methods_config_file) as f:
            methods_config = yaml.load(f, Loader=yaml.FullLoader)
        if "methods" not in methods_config.keys():
            raise KeyError("No 'methods' defined in config")
        for method_name, method_def in methods_config["methods"].items():
            builder.add_method(method_name, method_def)
    
    def _build_workflows(self, builder: SystemBuilder) -> None:
        if self.workflows_config_file is None:
            raise ValueError("Workflows config file is not set")
        with open(self.workflows_config_file) as f:
            workflows_config = yaml.load(f, Loader=yaml.FullLoader)
        if "workflows" not in workflows_config.keys():
            raise KeyError("No 'workflows' defined in config")
        for workflow_name, workflow_def in workflows_config["workflows"].items():
            builder.add_workflow(workflow_name, workflow_def)

    def build(self) -> System:
        builder = SystemBuilder()
        self._build_system(builder)
        self._build_labwares(builder)
        self._build_resources(builder)
        self._build_methods(builder)
        self._build_workflows(builder)
        return builder.build()