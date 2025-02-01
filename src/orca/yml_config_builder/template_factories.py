from typing import Dict, List, Optional, Tuple, Union
import logging
from orca.config_interfaces import ILabwareConfig, ILabwareThreadConfig, IMethodActionConfig, IMethodConfig, IResourceConfig, IResourcePoolConfig, ISystemConfig, IThreadStepConfig, IWorkflowConfig
from orca.resource_models.base_resource import Equipment, LabwareLoadableEquipment
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.location import Location
from orca.system.workflow_registry import WorkflowRegistry
from orca.scripting.scripting import IScriptRegistry
from orca.system.labware_registry_interfaces import ILabwareTemplateRegistry
from orca.system.thread_manager import IThreadManager
from orca.system.registries import LabwareRegistry
from orca.system.resource_registry import IResourceRegistry
from orca.system.system import System, SystemInfo
from orca.system.system_map import ILocationRegistry, IResourceLocator, SystemMap
from orca.system.registries import TemplateRegistry
from orca.system.template_registry_interfaces import IThreadTemplateRegistry, IMethodTemplateRegistry, IWorkflowTemplateRegistry
from orca.workflow_models.spawn_thread_action import SpawnThreadAction
from orca.workflow_models.labware_thread import IThreadObserver
from orca.yml_config_builder.configs import SystemConfigModel
from orca.yml_config_builder.dynamic_config import DynamicSystemConfigModel
from orca.yml_config_builder.resource_factory import IResourceFactory, ResourcePoolFactory
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.resource_models.transporter_resource import TransporterEquipment
from orca.workflow_models.workflow_templates import IMethodTemplate, JunctionMethodTemplate, ThreadTemplate, MethodActionTemplate, MethodTemplate, WorkflowTemplate
from orca.yml_config_builder.special_yml_parsing import get_dynamic_yaml_keys, is_dynamic_yaml
from orca.yml_config_builder.variable_resolution import VariablesRegistry


orca_logger = logging.getLogger("orca")
class LabwareConfigToTemplateAdapter:

    def get_template(self, name: str, config: ILabwareConfig) -> LabwareTemplate:
        labware = LabwareTemplate(name, config.type, config.options)
        return labware

class MethodActionConfigToTemplate:
    def __init__(self, resource_reg: IResourceRegistry, labware_temp_reg: ILabwareTemplateRegistry) -> None:
        self._resource_reg: IResourceRegistry = resource_reg
        self._labware_temp_reg: ILabwareTemplateRegistry = labware_temp_reg

    def get_template(self, name: str, config: IMethodActionConfig)  -> MethodActionTemplate:
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
        if config.outputs is None:
            outputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = inputs
        else:
            outputs = [self._labware_temp_reg.get_labware_template(name) for name in config.outputs]
        
        return MethodActionTemplate(resource,
                                    config.command,
                                    inputs,
                                    outputs, 
                                    config.options)


class MethodTemplateFactory:
    def __init__(self, resource_reg: IResourceRegistry, labware_template_reg: ILabwareTemplateRegistry) -> None:
        self._template_builder = MethodActionConfigToTemplate(resource_reg, labware_template_reg)

    def get_template(self, name: str, config: IMethodConfig) -> MethodTemplate:
        method = MethodTemplate(name, config.options)
        
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
                resource_locator: IResourceLocator,
                thread_manager: IThreadManager,
                script_reg: IScriptRegistry) -> None:
        self._labware_temp_reg: ILabwareTemplateRegistry = labware_temp_reg
        self._location_reg: ILocationRegistry = location_reg
        self._method_temp_reg: IMethodTemplateRegistry = method_temp_reg
        self._thread_temp_reg: IThreadTemplateRegistry = labware_thread_temp_reg
        self._resource_locator: IResourceLocator = resource_locator
        self._thread_manager: IThreadManager = thread_manager
        self._script_reg: IScriptRegistry = script_reg

    def get_template(self, 
                name: str, 
                config: ILabwareThreadConfig) -> ThreadTemplate:
        try:
            labware = self._get_labware_template(config)
            start_location = self._get_location(self._location_reg, config.start)                                            
            end_location = self._get_location(self._location_reg, config.end)
            observers: List[IThreadObserver] = [self._script_reg.get_script(script_name) for script_name in config.scripts]
            labware_thread = ThreadTemplate(labware, 
                                            start_location, 
                                            end_location, 
                                            observers)
        except KeyError as e:
            raise KeyError(f"Error building labware thread {name}") from e
        return labware_thread

    def _get_labware_template(self,
                            config: ILabwareThreadConfig) -> LabwareTemplate:
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
    
    def apply_methods(self, thread_template: ThreadTemplate, config: ILabwareThreadConfig) -> None:
        for step in config.steps:
            if isinstance(step, str):
                method_name = step
                method_resolver = self._get_method_resolver(method_name)
                thread_template.add_method(method_resolver)

            elif isinstance(step, IThreadStepConfig):
                method_resolver = self._get_method_resolver(step.method, step)
                thread_template.add_method(method_resolver)
            else:
                raise ValueError(f"Labware Thread {thread_template.name} has an invalid step type {step}.  Steps must be a string or a ThreadStepConfig")


    def _get_method_resolver(self, method_name: str, step_config: Optional[IThreadStepConfig] = None) -> IMethodTemplate:
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
                    spawn_action = SpawnThreadAction(self._thread_manager, thread_template)
                    method.add_method_observer(spawn_action)
        return method

class WorkflowTemplateFactory:
    def __init__(self,
                 thread_template_factory: ThreadTemplateFactory,
                 thread_temp_reg: IThreadTemplateRegistry) -> None:
        self._thread_temp_reg: IThreadTemplateRegistry = thread_temp_reg
        self._thread_template_factory = thread_template_factory

    def get_template(self, name: str, config: IWorkflowConfig) -> WorkflowTemplate:
        workflow = WorkflowTemplate(name)
        
        thread_templates: List[Tuple[ThreadTemplate, ILabwareThreadConfig]] = []
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

class ScriptsBuilder:
    def __init__(self, config: ISystemConfig, script_reg: IScriptRegistry) -> None:
        self._config: ISystemConfig = config
        self._scripting_registry = script_reg

    def build_scripts(self) -> None:
        if self._config.scripting is None:
            return
        for script_name, script_config in self._config.scripting.scripts.items():
            filepath, class_name = script_config.source.split(":")
            script = self._scripting_registry.create_script(filepath, class_name)
            self._scripting_registry.add_script(script_name, script)


class ResourcesBuilder:
    def __init__(self, config: ISystemConfig, resource_factory: IResourceFactory, resource_reg: IResourceRegistry) -> None:
        self._config: ISystemConfig = config
        self._resource_factory: IResourceFactory = resource_factory
        self._resource_reg: IResourceRegistry = resource_reg

    def build_resources(self) -> None:

        resource_pool_configs: Dict[str, IResourcePoolConfig] = {}

        # build resources from resource defs in config, defer resource pool creation
        for name, resource_config in self._config.resources.items():
            if resource_config.type == "pool" and isinstance(resource_config, IResourcePoolConfig):
                resource_pool_configs[name] = resource_config
                continue
            elif isinstance(resource_config, IResourceConfig):
                self._resource_reg.add_resource(self._resource_factory.create(name, resource_config))
            else:
                raise ValueError(f"Resource {name} has an invalid type {resource_config.type}")
            
        # build resource pools
        for name, resource_config in resource_pool_configs.items():
            pool = ResourcePoolFactory(self._resource_reg).create(name, resource_config)
            self._resource_reg.add_resource_pool(pool)

class LocationsBuilder:
    def __init__(self, config: ISystemConfig, resource_registry: IResourceRegistry, system_map: SystemMap) -> None:
        self._config: ISystemConfig = config
        self._resource_registry: IResourceRegistry = resource_registry
        self._system_map: SystemMap = system_map

    def build_locations(self) -> None:
        # build locations from location defs in config
        for location_name, location_config in self._config.locations.items():
            location = Location(location_config.teachpoint_name)
            location.set_options(location_config.options)  
            self._system_map.add_location(location)
        
        for res in self._resource_registry.resources:
            # skip resources like newtowrk switches, etc that don't have plate pad locations
            if isinstance(res, LabwareLoadableEquipment) \
                and not isinstance(res, EquipmentResourcePool) \
                and not isinstance(res, TransporterEquipment):
                # set resource to each location
                # if the plate-pad is not set in the resource definition, then use the resource name
                resource_config = self._config.resources[res.name]
                if not isinstance(resource_config, IResourceConfig):
                    raise ValueError(f"Resource {res.name} has an invalid type {resource_config.type}")
                if resource_config.plate_pad is not None:
                    location_name = resource_config.plate_pad
                else:
                    location_name = res.name

                try:
                    location = self._system_map.get_location(location_name)
                except KeyError:
                    raise LookupError(f"Location {location_name} referenced in resource {res.name} is not recognized.  Locations must be defined by the transporting resource.")
                location.resource = res





class LabwareTemplatesBuilder:
    def __init__(self, config: ISystemConfig, labware_template_registry: ILabwareTemplateRegistry) -> None:
        self._config = config
        self._adapter = LabwareConfigToTemplateAdapter()
        self._labware_template_registry = labware_template_registry
    
    def build_labware_templates(self) -> None:
        for labware_name, labware_config in self._config.labwares.items():
            labware_template = self._adapter.get_template(labware_name, labware_config)
            self._labware_template_registry.add_labware_template(labware_template)


class MethodTemplatesBuilder:
    def __init__(self, config: ISystemConfig, method_template_factory: MethodTemplateFactory, method_template_registry: IMethodTemplateRegistry) -> None:
        self._config: ISystemConfig = config
        self._method_template_factory: MethodTemplateFactory = method_template_factory
        self._method_template_registry: IMethodTemplateRegistry = method_template_registry

    def build_method_templates(self) -> None:
        for method_name, method_config in self._config.methods.items():
            method_template = self._method_template_factory.get_template(method_name, method_config)
            self._method_template_registry.add_method_template(method_template)
 
class WorkflowTemplatesBuilder:
    def __init__(self, 
                 config: ISystemConfig, 
                 workflow_template_factory: WorkflowTemplateFactory, 
                 workflow_template_registry: IWorkflowTemplateRegistry
                 ) -> None:
        self._config: ISystemConfig = config
        self._workflow_template_factory: WorkflowTemplateFactory = workflow_template_factory
        self._workflow_temp_reg: IWorkflowTemplateRegistry = workflow_template_registry

    def build_workflow_templates(self) -> None:
        for workflow_name, workflow_config in self._config.workflows.items():
            workflow_template = self._workflow_template_factory.get_template(workflow_name, workflow_config)
            self._workflow_temp_reg.add_workflow_template(workflow_template)



class ConfigToSystemBuilder:
    def __init__(self) -> None:
        
        self._config: Optional[ISystemConfig] = None
        self._scripting_registry: Optional[IScriptRegistry] = None
        self._system_info: Optional[SystemInfo] = None 
        self._system_map: Optional[SystemMap] = None
        self._resource_reg: Optional[IResourceRegistry] = None
        self._template_registry: Optional[TemplateRegistry] = None
        self._labware_registry: Optional[LabwareRegistry] = None
        self._thread_manager: Optional[IThreadManager] = None
        self._workflow_registry: Optional[WorkflowRegistry] = None
        self._resource_factory: Optional[IResourceFactory] = None
        self._method_template_factory: Optional[MethodTemplateFactory] = None
        self._workflow_template_factory: Optional[WorkflowTemplateFactory] = None
        self._variable_registry = VariablesRegistry()


    def set_config(self, config: SystemConfigModel) -> None:
        self._variable_registry.set_selector_configuration("labwares", config.labwares)
        self._variable_registry.set_selector_configuration("config", config.config)
        self._config = DynamicSystemConfigModel(config, self._variable_registry)

    def set_system_info(self, system_info: SystemInfo) -> None:
        self._system_info = system_info
    
    def set_system_map(self, system_map: SystemMap) -> None:
        self._system_map = system_map

    def set_resource_registry(self, resource_reg: IResourceRegistry) -> None:
        self._resource_reg = resource_reg

    def set_template_registry(self, template_registry: TemplateRegistry) -> None:
        self._template_registry = template_registry
    
    def set_labware_registry(self, labware_registry: LabwareRegistry) -> None:
        self._labware_registry = labware_registry

    def set_thread_manager(self, thread_manager: IThreadManager) -> None:
        self._thread_manager = thread_manager

    def set_workflow_registry(self, workflow_registry: WorkflowRegistry) -> None:
        self._workflow_registry = workflow_registry

    def set_script_registry(self, script_reg: IScriptRegistry) -> None:
        self._scripting_registry = script_reg
    
    def set_resource_factory(self, resource_factory: IResourceFactory) -> None:
        self._resource_factory = resource_factory

    def set_method_template_factory(self, method_template_factory: MethodTemplateFactory) -> None:
        self._method_template_factory = method_template_factory

    def set_workflow_template_factory(self, workflow_template_factory: WorkflowTemplateFactory) -> None:
        self._workflow_template_factory = workflow_template_factory
    
    def clear_registries(self) -> None:
        if self._resource_reg is not None:
            self._resource_reg.clear_resources()
        if self._template_registry is not None:
            self._template_registry.clear()
        if self._labware_registry is not None:
            self._labware_registry.clear()
        if self._workflow_registry is not None:
            self._workflow_registry.clear()
        if self._scripting_registry is not None:
            self._scripting_registry.clear()
        
    def get_system(self, deployment_stage: str) -> System:
        
        if self._config is None:
            raise ValueError("Config is not set.  You must set the config before building the system")
        if self._scripting_registry is None:
            raise ValueError("Script registry is not set.  You must set the script registry before building the system")
        if self._system_info is None:
            raise ValueError("System info is not set.  You must set the system info before building the system") 
        if self._system_map is None:
            raise ValueError("System map is not set.  You must set the system map before building the system")
        if self._resource_reg is None:
            raise ValueError("Resource registry is not set.  You must set the resource registry before building the system")
        if self._template_registry is None:
            raise ValueError("Template registry is not set.  You must set the template registry before building the system")
        if self._labware_registry is None:
            raise ValueError("Labware registry is not set.  You must set the labware registry before building the system")
        if self._thread_manager is None:
            raise ValueError("Thread manager is not set.  You must set the thread manager before building the system")
        if self._workflow_registry is None:
            raise ValueError("Workflow registry is not set.  You must set the workflow registry before building the system")
        if self._resource_factory is None:
            raise ValueError("Resource factory is not set.  You must set the resource factory before building the system")
        if self._method_template_factory is None:
            raise ValueError("Method template factory is not set.  You must set the method template factory before building the system")
        if self._workflow_template_factory is None:
            raise ValueError("Workflow template factory is not set.  You must set the workflow template factory before building the system")
        
        self._variable_registry.set_selector_configuration("opt", {"stage": deployment_stage})
        orca_logger.info(f"Building system for deployment stage '{deployment_stage}'...")
        
        system = System(self._system_info, 
                        self._system_map, 
                        self._resource_reg, 
                        self._template_registry, 
                        self._labware_registry, 
                        self._thread_manager, 
                        self._workflow_registry)

        
        self._scripting_registry.set_system(system)

        self._scripts_builder = ScriptsBuilder(self._config, self._scripting_registry)
        self._resources_builder = ResourcesBuilder(self._config, self._resource_factory, self._resource_reg)
        self._locations_builder = LocationsBuilder(self._config, self._resource_reg, self._system_map)
        self._labware_templates_builder = LabwareTemplatesBuilder(self._config, self._labware_registry)
        self._workflow_template_builder = WorkflowTemplatesBuilder(self._config, self._workflow_template_factory, self._template_registry)
        self._method_template_builder = MethodTemplatesBuilder(self._config, self._method_template_factory, self._template_registry)

        self._scripts_builder.build_scripts()
        self._resources_builder.build_resources()
        self._locations_builder.build_locations()
        self._labware_templates_builder.build_labware_templates()
        self._method_template_builder.build_method_templates()
        self._workflow_template_builder.build_workflow_templates()
        orca_logger.info("Completed System build")
        return system