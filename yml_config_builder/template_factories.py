
from typing import Dict, List, Optional, Tuple, Union

from resource_models.base_resource import Equipment
from resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from resource_models.location import Location
from system.labware_registry_interfaces import ILabwareTemplateRegistry
from system.registry_interfaces import IThreadRegistry
from system.registries import InstanceRegistry, LabwareRegistry
from system.resource_registry import IResourceRegistry, ResourceRegistry
from system.system import System, SystemInfo
from system.system_map import ILocationRegistry, IResourceLocator, SystemMap
from system.registries import TemplateRegistry
from system.template_registry_interfaces import IThreadTemplateRegistry, IMethodTemplateRegistry, IWorkflowTemplateRegistry
from workflow_models.spawn_thread_action import SpawnThreadAction
from yml_config_builder.configs import LabwareConfig, LabwareThreadConfig, MethodActionConfig, MethodConfig, ResourceConfig, ResourcePoolConfig, SystemConfig, ThreadStepConfig, WorkflowConfig
from yml_config_builder.resource_factory import ResourceFactory, ResourcePoolFactory
from resource_models.resource_pool import EquipmentResourcePool
from resource_models.transporter_resource import TransporterResource
from workflow_models.workflow_templates import IMethodTemplate, JunctionMethodTemplate, ThreadTemplate, MethodActionTemplate, MethodTemplate, WorkflowTemplate
from yml_config_builder.special_yml_parsing import get_dynamic_yaml_keys, is_dynamic_yaml


class LabwareConfigToTemplateAdapter:

    def get_template(self, name: str, config: LabwareConfig) -> LabwareTemplate:
        labware = LabwareTemplate(name, config.type, config.model_extra)
        return labware

class MethodActionConfigToTemplate:
    def __init__(self, resource_reg: IResourceRegistry, labware_temp_reg: ILabwareTemplateRegistry) -> None:
        self._resource_reg: IResourceRegistry = resource_reg
        self._labware_temp_reg: ILabwareTemplateRegistry = labware_temp_reg

    def get_template(self, name: str, config: MethodActionConfig)  -> MethodActionTemplate:
        # Get the resource name
        resource_name = config.resource
        if resource_name is None:
            resource_name = name
        
        # Get the equipment or resource pool object
        try:
            resource: Equipment | EquipmentResourcePool = self._resource_reg.get_equipment(resource_name)
            
        except KeyError:
            try:
                resource = self._resource_reg.get_resource_pool(resource_name)
            except KeyError:
                raise LookupError(f"The resource name '{resource_name}' in method actions is not recognized as a defined resource or resource pool")
        if not isinstance(resource, Equipment) and not isinstance(resource, EquipmentResourcePool):
            raise TypeError(f"The resource name '{resource_name}' in method actions is not an Equipment or EquipmentResourcePool")

        # Get the input labware templates
        inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = []
        for input in config.inputs:
            if input == "$ANY":
                inputs.append(AnyLabwareTemplate())
            else:
                try:
                    labware_template = self._labware_temp_reg.get_labware_template(input)
                except KeyError:
                    raise LookupError(f"The input labware name '{input}' in method actions is not recognized as a defined labware.  Input must be a recognized labware or keyword")
                inputs.append(labware_template)
        # Get the output labware templates
        outputs = [self._labware_temp_reg.get_labware_template(name) for name in config.outputs]
        
        return MethodActionTemplate(resource,
                                    config.command,
                                    inputs,
                                    outputs, 
                                    config.model_extra)

class MethodTemplateFactory:
    def __init__(self, resource_reg: IResourceRegistry, labware_template_reg: ILabwareTemplateRegistry) -> None:
        self._template_builder = MethodActionConfigToTemplate(resource_reg, labware_template_reg)

    def get_template(self, name: str, config: MethodConfig, ) -> MethodTemplate:
        method = MethodTemplate(name, config.model_extra)
        
        for action_item in config.actions:
            for action_name, action_config in action_item.items():
                method_action_template = self._template_builder.get_template(action_name, action_config)
                method.append_action(method_action_template)
        return method    


## Workflow building

class ThreadTemplateFactory:
    def __init__(self,
                labware_temp_reg: ILabwareTemplateRegistry, 
                location_reg: ILocationRegistry, 
                method_temp_reg: IMethodTemplateRegistry, 
                labware_thread_temp_reg: IThreadTemplateRegistry, 
                thread_reg: IThreadRegistry,
                resource_locator: IResourceLocator) -> None:
        self._labware_temp_reg: ILabwareTemplateRegistry = labware_temp_reg
        self._location_reg: ILocationRegistry = location_reg
        self._method_temp_reg: IMethodTemplateRegistry = method_temp_reg
        self._thread_temp_reg: IThreadTemplateRegistry = labware_thread_temp_reg
        self._thread_reg: IThreadRegistry = thread_reg
        self._resource_locator: IResourceLocator = resource_locator

    def get_template(self, 
                name: str, 
                config: LabwareThreadConfig) -> ThreadTemplate:
        try:
            labware = self._get_labware_template(config)
            start_location = self._get_location(self._location_reg, config.start)                                            
            end_location = self._get_location(self._location_reg, config.end)
            # make labware thread
            labware_thread = ThreadTemplate(labware_template=labware, start=start_location, end=end_location)
        except KeyError as e:
            raise KeyError(f"Error building labware thread {name}") from e
        return labware_thread

    def _get_labware_template(self,
                            config: LabwareThreadConfig) -> LabwareTemplate:
        try:
            return self._labware_temp_reg.get_labware_template(config.labware)
        except KeyError as e:
            raise KeyError(f"Labware {config.labware} is not in the system labwares") from e

    def _get_location(self, location_registry: ILocationRegistry, location_name: str) -> Location:
        try:
            return location_registry.get_location(location_name)
        except KeyError:
            try:
                return self._resource_locator.get_resource_location(location_name)
            except KeyError:
                 raise KeyError(f"Start/end location {location_name} which is not in the system locations or system resources")
    
    def apply_methods(self, thread_template: ThreadTemplate, config: LabwareThreadConfig) -> None:
        for step in config.steps:
            if isinstance(step, str):
                method_name = step
                method_resolver = self._get_method_resolver(method_name)
                thread_template.add_method(method_resolver)

            elif isinstance(step, ThreadStepConfig):
                method_resolver = self._get_method_resolver(step.method, step)
                thread_template.add_method(method_resolver)
            else:
                raise ValueError(f"Labware Thread {thread_template.name} has an invalid step type {step}.  Steps must be a string or a ThreadStepConfig")


    def _get_method_resolver(self, method_name: str, step_config: Optional[ThreadStepConfig] = None) -> IMethodTemplate:
        if is_dynamic_yaml(method_name):
            input_index = [int(key) for key in get_dynamic_yaml_keys(method_name)]
            shared_method = JunctionMethodTemplate()
            return shared_method

        try: 
            method = self._method_temp_reg.get_method_template(method_name)
        except KeyError:
            raise LookupError(f"Method {method_name} is not defined.  Method {method_name} must be defined")
        if step_config is not None:
            if step_config.spawn:
                for thread_name in step_config.spawn:
                    thread_template = self._thread_temp_reg.get_labware_thread_template(thread_name)
                    spawn_action = SpawnThreadAction(self._thread_reg, thread_template)
                    method.add_method_observer(spawn_action)
        return method

class WorkflowTemplateFactory:
    def __init__(self,
                 thread_template_factory: ThreadTemplateFactory,
                 thread_temp_reg: IThreadTemplateRegistry) -> None:
        self._thread_temp_reg: IThreadTemplateRegistry = thread_temp_reg
        self._thread_template_factory = thread_template_factory

    def get_template(self, name: str, config: WorkflowConfig) -> WorkflowTemplate:
        workflow = WorkflowTemplate(name)
        
        thread_templates: List[Tuple[ThreadTemplate, LabwareThreadConfig]] = []
        # initialize thread templates
        for thread_name, thread_config in config.threads.items():
            thread_template = self._thread_template_factory.get_template(thread_name, thread_config)
            thread_templates.append((thread_template, thread_config))

        # register thread templates
        for thread_template, _ in thread_templates:
            self._thread_temp_reg.add_labware_thread_template(thread_template)

        # appply methods to thread templates
        for thread_template, thread_config in thread_templates:
            self._thread_template_factory.apply_methods(thread_template, thread_config)
            
            if thread_config.type == "start":
                workflow.add_thread(thread_template, is_start=True)
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
        adapter = LabwareConfigToTemplateAdapter()
        for labware_name, labware_config in self._config.labwares.items():
            labware_template = adapter.get_template(labware_name, labware_config)
            labware_temp_reg.add_labware_template(labware_template)

    def _build_method_templates(self, method_template_factory: MethodTemplateFactory, method_temp_Reg: IMethodTemplateRegistry) -> None:
        for method_name, method_config in self._config.methods.items():
            method_template = method_template_factory.get_template(method_name, method_config)
            method_temp_Reg.add_method_template( method_template)

    def _build_workflow_templates(self, workflow_template_factory: WorkflowTemplateFactory, workflow_temp_reg: IWorkflowTemplateRegistry) -> None:
        for workflow_name, workflow_config in self._config.workflows.items():
            workflow_template = workflow_template_factory.get_template(workflow_name, workflow_config,)
            workflow_temp_reg.add_workflow_template(workflow_template)

    def get_system(self) -> System:
        resource_reg = ResourceRegistry()
        system_map = SystemMap(resource_reg)
        template_registry = TemplateRegistry()
        labware_registry = LabwareRegistry()
        instance_registry = InstanceRegistry(labware_registry, system_map)
        system_info = SystemInfo(self._config.system.name, self._config.system.version, self._config.system.description, self._config.model_extra)
        system = System(system_info, system_map, resource_reg, template_registry, labware_registry, instance_registry)
        
        # TODO: Move factories out
        method_template_factory = MethodTemplateFactory(resource_reg, labware_registry)
        thread_template_factory = ThreadTemplateFactory(labware_registry, 
                                                           system_map, 
                                                           template_registry, 
                                                           template_registry, 
                                                           instance_registry, 
                                                           system_map)
        workflow_template_factory = WorkflowTemplateFactory(thread_template_factory, template_registry)
        self._build_resources(resource_reg)
        self._build_locations(resource_reg, system_map)
        self._build_labware_templates(labware_registry)
        self._build_method_templates(method_template_factory, template_registry)
        self._build_workflow_templates( workflow_template_factory, template_registry)
        return system

        