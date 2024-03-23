
from typing import Dict, List, Optional, Union
import yaml

from resource_models.base_resource import Equipment
from resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from resource_models.location import Location
from yml_config_builder.configs import LabwareConfig, LabwareThreadConfig, MethodActionConfig, MethodConfig, ResourceConfig, ResourcePoolConfig, SystemConfig, ThreadStepConfig, WorkflowConfig
from yml_config_builder.resource_factory import ResourceFactory, ResourcePoolFactory
from resource_models.resource_pool import EquipmentResourcePool
from resource_models.transporter_resource import TransporterResource
from workflow_models.workflow_templates import IMethodResolver, JunctionMethodTemplate, LabwareThreadTemplate, MethodActionTemplate, MethodTemplate, SystemTemplate, WorkflowTemplate
from yml_config_builder.special_yml_parsing import get_dynamic_yaml_keys, is_dynamic_yaml


class LabwareConfigToTemplateAdapter:
    def __init__(self, name: str, config: LabwareConfig) -> None:
        self._name = name
        self._config: LabwareConfig = config

    def get_template(self) -> LabwareTemplate:
        labware = LabwareTemplate(self._name, self._config.type, self._config.model_extra)
        return labware

class MethodActionConfigToTemplate:
    def __init__(self, name: str, config: MethodActionConfig) -> None:
        self._name = name
        self._config = config

    def get_template(self, system_template: SystemTemplate) -> MethodActionTemplate:
        # Get the resource name
        resource_name = self._config.resource
        if resource_name is None:
            resource_name = self._name
        
        # Get the equipment or resource pool object
        if self._name in system_template.equipment.keys():
            resource: Equipment | EquipmentResourcePool = system_template.equipment[resource_name]
        elif resource_name in system_template.resource_pools.keys():
            resource = system_template.resource_pools[resource_name]
        else:
            raise LookupError(f"The resource name '{resource_name}' in method actions is not recognized as a defined resource or resource pool")
        
        # Get the input labware templates
        inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = []
        for input in self._config.inputs:
            if input == "$ANY":
                inputs.append(AnyLabwareTemplate())
            elif input in system_template.labware_templates.keys():
                inputs.append(system_template.labware_templates[input])
            else:
                raise LookupError(f"The input labware name '{input}' in method actions is not recognized as a defined labware.  Input must be a recognized labware or keyword")
            
        # Get the output labware templates
        outputs = [system_template.labware_templates[name] for name in self._config.outputs]

        return MethodActionTemplate(resource,
                                     self._config.command,
                                         inputs,
                                           outputs, 
                                           self._config.model_extra,)

class MethodConfigToTemplate:
    def __init__(self, name: str, config: MethodConfig) -> None:
        self._name = name
        self._config = config

    def get_template(self, system_template: SystemTemplate) -> MethodTemplate:
        method = MethodTemplate(self._name, self._config.model_extra)
        for action_item in self._config.actions:
            for action_name, action_config in action_item.items():
                method_action_template = MethodActionConfigToTemplate(action_name, action_config).get_template(system_template)
                method.append_action(method_action_template)
        return method    


## Workflow building

class LabwareThreadConfigToTemplateAdapter:
    def __init__(self, name: str, config: LabwareThreadConfig) -> None:
        self._name = name
        self._config = config

    def get_template(self, system_template: SystemTemplate) -> LabwareThreadTemplate:

        labware = self._get_labware(system_template)
        start_location = self._get_location(self._config.start, system_template)
        end_location = self._get_location(self._config.end, system_template)
        # make labware thread
        labware_thread = LabwareThreadTemplate(labware=labware, start=start_location, end=end_location)


        for step in self._config.steps:
            if isinstance(step, str):
                method_name = step
                method_resolver = self._get_method_resolver(method_name, system_template)
                labware_thread.add_method(method_resolver)

            elif isinstance(step, ThreadStepConfig):
                method_resolver = self._get_method_resolver(step.method, system_template, step)
                labware_thread.add_method(method_resolver)
            else:
                raise ValueError(f"Labware Thread {self._name} has an invalid step type {step}.  Steps must be a string or a ThreadStepConfig")

        return labware_thread

    def _get_labware(self, system_template: SystemTemplate) -> LabwareTemplate:
        if self._config.labware not in system_template.labware_templates.keys():
            raise KeyError(f"Labware Thread {self._name} defines labware {self._config.labware} which is not in the system labwares")
        return system_template.labware_templates[self._config.labware]

    def _get_location(self, location_name: str, system_template: SystemTemplate) -> Location:
        if location_name in system_template.locations.keys():
            return system_template.locations[location_name]
        elif location_name in system_template.resources.keys():
            return system_template.get_resource_location(location_name)
        else:
            raise KeyError(f"Labware Thread {self._name} defines a start/end location {location_name} which is not in the system locations or system resources")
    
    def _get_method_resolver(self, method_name: str, system_template: SystemTemplate, step_config: Optional[ThreadStepConfig] = None) -> IMethodResolver:
        if is_dynamic_yaml(method_name):
            input_index = [int(key) for key in get_dynamic_yaml_keys(method_name)]
            shared_method = JunctionMethodTemplate()
            return shared_method

        if method_name not in system_template.method_templates.keys():
            raise LookupError(f"Method {method_name} referenced in labware thread {self._name} is not defined.  Method {method_name} must be defined")
        method = system_template.method_templates[method_name]
        if step_config is not None:
            if step_config.spawn:
                # TODO: figure out how to attach the actual thread here
                method.set_spawn(step_config.spawn)
        return method

class WorkflowConfigToTemplateAdapter:
    def __init__(self, name: str, config: WorkflowConfig) -> None:
        self._name = name
        self._config = config

    def get_template(self, system: SystemTemplate) -> WorkflowTemplate:
        workflow = WorkflowTemplate(self._name)
        for thread_name, thread_config in self._config.threads.items():
            workflow.labware_threads[thread_name] = LabwareThreadConfigToTemplateAdapter(thread_name, thread_config).get_template(system)
        return workflow

class SystemConfigToTemplateAdapter:
    def __init__(self, config: SystemConfig) -> None:
        self._config = config

    def _build_resources(self, system_template: SystemTemplate) -> None:
        resource_pool_configs: Dict[str, ResourcePoolConfig] = {}

        # build resources from resource defs in config, defer resource pool creation
        for name, resource_config in self._config.resources.items():
            if resource_config.type == "pool" and isinstance(resource_config, ResourcePoolConfig):
                resource_pool_configs[name] = resource_config
                continue
            elif isinstance(resource_config, ResourceConfig):
                system_template.add_resource(name, ResourceFactory().create(name, resource_config))
            else:
                raise ValueError(f"Resource {name} has an invalid type {resource_config.type}")
            
        # build resource pools
        for name, resource_config in resource_pool_configs.items():
            pool = ResourcePoolFactory(system_template).create(name, resource_config)
            system_template.add_resource_pool(name, pool)

    def _build_locations(self, system_template: SystemTemplate) -> None:
        # build locations from location defs in config
        for location_name, location_config in self._config.locations.items():
             location = Location(location_config.teachpoint_name)
             location.set_options(location_config.model_extra)  

        # build locations from labware transporters
        for _, transporter in system_template.labware_transporters.items():
            taught_positions = transporter.get_taught_positions()
            for loc_name in taught_positions:
                if loc_name not in system_template.locations.keys():
                    system_template.locations[loc_name] = Location(loc_name)
                    

        for _, res in system_template.resources.items():
            # skip resources like newtowrk switches, etc that don't have plate pad locations
            if isinstance(res, Equipment) \
                and not isinstance(res, EquipmentResourcePool) \
                and not isinstance(res, TransporterResource):
                # set resource to each location
                # if the plate-pad is not set in the resource definition, then use the resource name
                resource_config = self._config.resources[res.name]
                if not isinstance(resource_config, ResourceConfig):
                    raise ValueError(f"Resource {res.name} has an invalid type {resource_config.type}")
                if resource_config.plate_pad is not None:
                    location_name = resource_config.plate_pad
                else:
                    location_name = res.name
                if location_name not in system_template.locations.keys():
                    raise LookupError(f"Location {location_name} referenced in resource {res.name} is not recognized.  Locations must be defined by the transporting resource.")
                system_template.locations[location_name].resource = res

    def _build_labware_templates(self, system_template: SystemTemplate) -> None:
        for labware_name, labware_config in self._config.labwares.items():
            labware_template = LabwareConfigToTemplateAdapter(labware_name, labware_config).get_template()
            system_template.add_labware(labware_name, labware_template)

    def _build_method_templates(self, system_template: SystemTemplate) -> None:
        for method_name, method_config in self._config.methods.items():
            method_template = MethodConfigToTemplate(method_name, method_config).get_template(system_template)
            system_template.add_method_template(method_name, method_template)

    def _build_workflow_templates(self, system_template: SystemTemplate) -> None:
        for workflow_name, workflow_config in self._config.workflows.items():
            workflow_template = WorkflowConfigToTemplateAdapter(workflow_name, workflow_config).get_template(system_template)
            system_template.add_workflow_template(workflow_name, workflow_template)

    def get_system_template(self) -> SystemTemplate:
        template = SystemTemplate(self._config.system.name, self._config.system.version, self._config.system.description, self._config.model_extra)
        self._build_resources(template)
        self._build_locations(template)
        self._build_labware_templates(template)
        self._build_method_templates(template)
        self._build_workflow_templates(template)
        return template

class ConfigFile:
    def __init__(self, path: str) -> None:
        self._path = path
        with open(self._path, 'r') as f:
            self._yml = yaml.load(f, Loader=yaml.FullLoader)
        self._config = SystemConfig.model_validate(self._yml)
    
    def get_system_template(self) -> SystemTemplate:
        return SystemConfigToTemplateAdapter(self._config).get_system_template()
        
