
from typing import Dict, List, Optional, Union
import yaml

from resource_models.base_resource import Equipment
from resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from resource_models.location import Location
from system.registry_interfaces import ILabwareTemplateRegistry
from system.registries import InstanceRegistry, LabwareRegistry
from system.resource_registry import IResourceRegistry, ResourceRegistry
from system.system import System, SystemInfo
from system.system_map import ILocationRegistry, IResourceLocator, SystemMap
from system.registries import TemplateRegistry
from system.template_registry_interfaces import IMethodTemplateRegistry, IWorkflowTemplateRegistry
from yml_config_builder.configs import LabwareConfig, LabwareThreadConfig, MethodActionConfig, MethodConfig, ResourceConfig, ResourcePoolConfig, SystemConfig, ThreadStepConfig, WorkflowConfig
from yml_config_builder.resource_factory import ResourceFactory, ResourcePoolFactory
from resource_models.resource_pool import EquipmentResourcePool
from resource_models.transporter_resource import TransporterResource
from workflow_models.workflow_templates import IMethodResolver, JunctionMethodTemplate, LabwareThreadTemplate, MethodActionTemplate, MethodTemplate, WorkflowTemplate
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

    def get_template(self, resource_reg: IResourceRegistry, labware_temp_reg: ILabwareTemplateRegistry) -> MethodActionTemplate:
        # Get the resource name
        resource_name = self._config.resource
        if resource_name is None:
            resource_name = self._name
        
        # Get the equipment or resource pool object
        try:
            resource: Equipment | EquipmentResourcePool = resource_reg.get_equipment(resource_name)
            
        except KeyError:
            try:
                resource = resource_reg.get_resource_pool(resource_name)
            except KeyError:
                raise LookupError(f"The resource name '{resource_name}' in method actions is not recognized as a defined resource or resource pool")
        if not isinstance(resource, Equipment) and not isinstance(resource, EquipmentResourcePool):
            raise TypeError(f"The resource name '{resource_name}' in method actions is not an Equipment or EquipmentResourcePool")

        # Get the input labware templates
        inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = []
        for input in self._config.inputs:
            if input == "$ANY":
                inputs.append(AnyLabwareTemplate())
            else:
                try:
                    labware_template = labware_temp_reg.get_labware_template(input)
                except KeyError:
                    raise LookupError(f"The input labware name '{input}' in method actions is not recognized as a defined labware.  Input must be a recognized labware or keyword")
                inputs.append(labware_template)
        # Get the output labware templates
        outputs = [labware_temp_reg.get_labware_template(name) for name in self._config.outputs]
        
        return MethodActionTemplate(resource,
                                     self._config.command,
                                         inputs,
                                           outputs, 
                                           self._config.model_extra,)

class MethodConfigToTemplate:
    def __init__(self, name: str, config: MethodConfig) -> None:
        self._name = name
        self._config = config

    def get_template(self, resource_reg: IResourceRegistry, labware_temp_reg: ILabwareTemplateRegistry) -> MethodTemplate:
        method = MethodTemplate(self._name, self._config.model_extra)
        for action_item in self._config.actions:
            for action_name, action_config in action_item.items():
                template_builder = MethodActionConfigToTemplate(action_name, action_config)
                method_action_template = template_builder.get_template(resource_reg, labware_temp_reg)
                method.append_action(method_action_template)
        return method    


## Workflow building

class LabwareThreadConfigToTemplateAdapter:
    def __init__(self, name: str, config: LabwareThreadConfig) -> None:
        self._name = name
        self._config = config

    def get_template(self, labware_temp_reg: ILabwareTemplateRegistry, location_reg: ILocationRegistry, method_temp_reg: IMethodTemplateRegistry, resource_locator: IResourceLocator) -> LabwareThreadTemplate:

        labware = self._get_labware_template(labware_temp_reg)
        start_location = self._get_location(self._config.start, location_reg, resource_locator)
        end_location = self._get_location(self._config.end, location_reg, resource_locator)
        # make labware thread
        labware_thread = LabwareThreadTemplate(labware_template=labware, start=start_location, end=end_location)


        for step in self._config.steps:
            if isinstance(step, str):
                method_name = step
                method_resolver = self._get_method_resolver(method_name, method_temp_reg)
                labware_thread.add_method(method_resolver)

            elif isinstance(step, ThreadStepConfig):
                method_resolver = self._get_method_resolver(step.method, method_temp_reg, step)
                labware_thread.add_method(method_resolver)
            else:
                raise ValueError(f"Labware Thread {self._name} has an invalid step type {step}.  Steps must be a string or a ThreadStepConfig")

        return labware_thread

    def _get_labware_template(self, labware_temp_reg: ILabwareTemplateRegistry) -> LabwareTemplate:
        try:
            return labware_temp_reg.get_labware_template(self._config.labware)
        except KeyError as e:
            raise KeyError(f"Labware Thread {self._name} defines labware {self._config.labware} which is not in the system labwares") from e

    def _get_location(self, location_name: str, location_registry: ILocationRegistry, resource_locator: IResourceLocator) -> Location:
        try:
            return location_registry.get_location(location_name)
        except KeyError:
            try:
                return resource_locator.get_resource_location(location_name)
            except KeyError:
                 raise KeyError(f"Labware Thread {self._name} defines a start/end location {location_name} which is not in the system locations or system resources")
           
    
    def _get_method_resolver(self, method_name: str, method_temp_reg: IMethodTemplateRegistry, step_config: Optional[ThreadStepConfig] = None) -> IMethodResolver:
        if is_dynamic_yaml(method_name):
            input_index = [int(key) for key in get_dynamic_yaml_keys(method_name)]
            shared_method = JunctionMethodTemplate()
            return shared_method

        try: 
            method = method_temp_reg.get_method_template(method_name)
        except KeyError:
            raise LookupError(f"Method {method_name} referenced in labware thread {self._name} is not defined.  Method {method_name} must be defined")
        if step_config is not None:
            if step_config.spawn:
                # TODO: figure out how to attach the actual thread here
                method.set_spawn(step_config.spawn)
        return method

class WorkflowConfigToTemplateAdapter:
    def __init__(self, name: str, config: WorkflowConfig) -> None:
        self._name = name
        self._config = config

    def get_template(self, labware_temp_reg: ILabwareTemplateRegistry, location_reg: ILocationRegistry, method_temp_reg: IMethodTemplateRegistry, resource_locator: IResourceLocator) -> WorkflowTemplate:
        workflow = WorkflowTemplate(self._name)
        for thread_name, thread_config in self._config.threads.items():
            workflow.labware_threads[thread_name] = LabwareThreadConfigToTemplateAdapter(thread_name, thread_config).get_template(labware_temp_reg, location_reg, method_temp_reg, resource_locator)
        return workflow

class ConfigToSystemBuilder:
    def __init__(self, config: SystemConfig) -> None:
        self._config = config

    def _build_resources(self, resource_reg: IResourceRegistry) -> None:
        resource_pool_configs: Dict[str, ResourcePoolConfig] = {}

        # build resources from resource defs in config, defer resource pool creation
        for name, resource_config in self._config.resources.items():
            if resource_config.type == "pool" and isinstance(resource_config, ResourcePoolConfig):
                resource_pool_configs[name] = resource_config
                continue
            elif isinstance(resource_config, ResourceConfig):
                resource_reg.add_resource(ResourceFactory().create(name, resource_config))
            else:
                raise ValueError(f"Resource {name} has an invalid type {resource_config.type}")
            
        # build resource pools
        for name, resource_config in resource_pool_configs.items():
            pool = ResourcePoolFactory(resource_reg).create(name, resource_config)
            resource_reg.add_resource_pool(pool)

    def _build_locations(self, resource_registry: IResourceRegistry, system_map: SystemMap) -> None:
        # build locations from location defs in config
        for location_name, location_config in self._config.locations.items():
            location = Location(location_config.teachpoint_name)
            location.set_options(location_config.model_extra)  
            system_map.add_location(location)
        

        # TODO:  resource_config should probably be in Resource and this all moved to SystemMap
        for res in resource_registry.resources:
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

                try:
                    location = system_map.get_location(location_name)
                except KeyError:
                    raise LookupError(f"Location {location_name} referenced in resource {res.name} is not recognized.  Locations must be defined by the transporting resource.")
                location.resource = res

    def _build_labware_templates(self, labware_temp_reg: ILabwareTemplateRegistry) -> None:
        for labware_name, labware_config in self._config.labwares.items():
            labware_template = LabwareConfigToTemplateAdapter(labware_name, labware_config).get_template()
            labware_temp_reg.add_labware_template(labware_template)

    def _build_method_templates(self, method_temp_Reg: IMethodTemplateRegistry, resource_reg: IResourceRegistry, labware_temp_reg: ILabwareTemplateRegistry) -> None:
        for method_name, method_config in self._config.methods.items():
            method_template = MethodConfigToTemplate(method_name, method_config).get_template(resource_reg, labware_temp_reg)
            method_temp_Reg.add_method_template( method_template)

    def _build_workflow_templates(self, workflow_temp_reg: IWorkflowTemplateRegistry, labware_temp_reg: ILabwareTemplateRegistry, location_reg: ILocationRegistry, method_temp_reg: IMethodTemplateRegistry, resource_locator: IResourceLocator) -> None:
        for workflow_name, workflow_config in self._config.workflows.items():
            workflow_template = WorkflowConfigToTemplateAdapter(workflow_name, workflow_config).get_template(labware_temp_reg, location_reg, method_temp_reg,resource_locator)
            workflow_temp_reg.add_workflow_template(workflow_template)

    def get_system(self) -> System:
        resource_reg = ResourceRegistry()
        self._build_resources(resource_reg)
        system_map = SystemMap(resource_reg)
        self._build_locations(resource_reg, system_map)
        template_registry = TemplateRegistry()
        labware_registry = LabwareRegistry()
        self._build_labware_templates(labware_registry)
        self._build_method_templates(template_registry, resource_reg, labware_registry)
        self._build_workflow_templates(template_registry, labware_registry, system_map, template_registry, system_map)
        system_info = SystemInfo(self._config.system.name, self._config.system.version, self._config.system.description, self._config.model_extra)
        instance_registry = InstanceRegistry()
        system = System(system_info, system_map, resource_reg, template_registry, labware_registry, instance_registry)
        return system

class ConfigFile:
    def __init__(self, path: str) -> None:
        self._path = path
        with open(self._path, 'r') as f:
            self._yml = yaml.load(f, Loader=yaml.FullLoader)
        self._config = SystemConfig.model_validate(self._yml)
    
    def get_system(self) -> System:
        return ConfigToSystemBuilder(self._config).get_system()
        
