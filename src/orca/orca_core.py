import os
from typing import Any, Callable, Coroutine, Dict, List, Optional
import logging
import asyncio

import yaml

from orca.driver_management.driver_installer import DriverInstaller, DriverLoader, DriverManager, InstalledDriverRegistry, RemoteAvailableDriverRegistry
from orca.helper import FilepathReconciler
from orca.scripting.scripting import IScriptFactory, IScriptRegistry, NullScriptFactory, ScriptFactory, ScriptRegistry
from orca.system.method_executor import MethodExecutor
from orca.resource_models.base_resource import IInitializableResource
from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.system.move_handler import MoveHandler
from orca.system.registries import LabwareRegistry, TemplateRegistry
from orca.system.reservation_manager import ReservationManager
from orca.system.resource_registry import ResourceRegistry
from orca.system.system import ISystem, SystemInfo
from orca.system.system_map import SystemMap
from orca.system.thread_manager import ThreadManagerFactory
from orca.system.workflow_registry import WorkflowRegistry
from orca.yml_config_builder.configs import SystemConfigModel
from orca.yml_config_builder.template_factories import ConfigToSystemBuilder, MethodTemplateFactory, ThreadTemplateFactory, WorkflowTemplateFactory
from orca.yml_config_builder.resource_factory import IResourceFactory, ResourceFactory



PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
orca_logger = logging.getLogger("orca")
class OrcaCore:

    def __init__(self, 
                 config_filepath: str, 
                 driver_manager: DriverManager,
                 scripting_registry: Optional[IScriptRegistry] = None,
                 resource_factory: Optional[IResourceFactory] = None,
                 deployment_stage: str = "prod"
                 ) -> None:
        self._config_filepath = config_filepath
        with open( self._config_filepath, "r") as f:
            yml_content = f.read()
        self._yml = yaml.load(yml_content, Loader=yaml.FullLoader) # type: ignore
        self._config = SystemConfigModel.model_validate(self._yml)

        # set scripting registry
        if scripting_registry is None:
            absolute_path = os.path.abspath(config_filepath)
            directory_path = os.path.dirname(absolute_path)
            file_reconciler = FilepathReconciler(directory_path)
            # if no scripting is defined, create a null scripting factory
            if self._config.scripting == {}:
                scripting_factory: IScriptFactory = NullScriptFactory()
                scripting_registry = ScriptRegistry(scripting_factory)
            else:
                scripting_factory = ScriptFactory(self._config.scripting, file_reconciler)
                scripting_registry = ScriptRegistry(scripting_factory)
        self._scripting_registry: IScriptRegistry = scripting_registry

        # set resource factory
        if resource_factory is None:
            absolute_path = os.path.abspath(config_filepath)
            directory_path = os.path.dirname(absolute_path)
            filepath_reconciler = FilepathReconciler(directory_path)
            resource_factory = ResourceFactory(driver_manager, filepath_reconciler)
        self._resource_factory: IResourceFactory = resource_factory

        resource_reg = ResourceRegistry()
        system_map = SystemMap(resource_reg)
        template_registry = TemplateRegistry()
        labware_registry = LabwareRegistry()
        reservation_manager = ReservationManager(system_map) 
        move_handler = MoveHandler(reservation_manager, labware_registry, system_map)
        thread_manager = ThreadManagerFactory.create_instance(labware_registry, reservation_manager, system_map, move_handler)
        workflow_registry = WorkflowRegistry(thread_manager, labware_registry, system_map)
        system_info = SystemInfo(self._config.system.name, 
                                 self._config.system.version, 
                                 self._config.system.description, 
                                 dict(self._config.options.__dict__))
        method_template_factory = MethodTemplateFactory(resource_reg, labware_registry)
        thread_template_factory = ThreadTemplateFactory(labware_registry, 
                                                        system_map, 
                                                        template_registry, 
                                                        template_registry, 
                                                        system_map,
                                                        thread_manager,
                                                        self._scripting_registry)
        workflow_template_factory = WorkflowTemplateFactory(thread_template_factory, template_registry)

        self._builder = ConfigToSystemBuilder()
        self._builder.set_config(self._config)
        self._builder.set_system_info(system_info)
        self._builder.set_system_map(system_map)
        self._builder.set_script_registry(self._scripting_registry)
        self._builder.set_resource_registry(resource_reg)
        self._builder.set_template_registry(template_registry)
        self._builder.set_labware_registry(labware_registry)
        self._builder.set_thread_manager(thread_manager)
        self._builder.set_workflow_registry(workflow_registry)
        self._builder.set_resource_factory(resource_factory)
        self._builder.set_method_template_factory(method_template_factory)
        self._builder.set_workflow_template_factory(workflow_template_factory)
        self._method_executor_registry: Dict[str, MethodExecutor] = {}
        self._system = self._builder.get_system(deployment_stage)

    @property
    def system(self) -> ISystem:
        return self._system
    
    @property
    def system_config(self) -> SystemConfigModel:
        return self._config

    async def initialize(self,
                   resource_list: Optional[List[str]] = None,
                   deployment_stage: Optional[str] = None) -> None:
        if deployment_stage is not None:
            self._builder.clear_registries()
            self._system = self._builder.get_system(deployment_stage)
        if resource_list is None:
            await self._system.initialize_all()
        else:
            init_fxns: List[Callable[[], Coroutine[Any, Any, None]]] = []
            for resource_name in resource_list:
                resource = self._system.get_resource(resource_name)
                if isinstance(resource, IInitializableResource):
                    init_fxns.append(resource.initialize)
            await asyncio.gather(*[f() for f in init_fxns])

    def create_workflow_instance(self, 
                                 workflow_name: str, 
                                 deployment_stage: Optional[str] = None,
                                 ) -> str:
        if deployment_stage is not None:
            self._builder.clear_registries()
            self._system = self._builder.get_system(deployment_stage)
        workflow_template = self._system.get_workflow_template(workflow_name)
        workflow = self._system.create_workflow_instance(workflow_template)
        self._system.add_workflow(workflow)
        return workflow.id

    async def run_workflow(self, workflow_id: str) -> None:
        workflow = self._system.get_workflow(workflow_id)
        # TODO:  Add check to make sure system is initailized
        await self.initialize()
        await workflow.start()


    def create_method_instance(self, 
                               method_name: str, 
                               start_map: Dict[str, str], 
                               end_map: Dict[str, str], 
                               deployment_stage: Optional[str] = None,
                               ) -> str:
        if deployment_stage is not None:
            self._builder.clear_registries()
            self._system = self._builder.get_system(deployment_stage)
        try:
            method_template = self._system.get_method_template(method_name)
        except KeyError:
            raise LookupError(f"Method {method_name} is not defined with then System.  Make sure it is included in the config file and the config file loaded correctly.")

        def labware_location_hook(d: Dict[str, str]):
            conversion: Dict[LabwareTemplate, Location] = {}
            for key, value in d.items():
                try:
                    template = self._system.get_labware_template(key)
                except KeyError:
                    raise ValueError(f"Labware {key} is not defined in the system")
                try:
                    location = self._system.get_location(value)
                except KeyError:
                    raise ValueError(f"Location {value} is not defined in the system")
                conversion[template] = location
            return conversion

        start_map_obj: Dict[LabwareTemplate, Location] = labware_location_hook(start_map)
        end_map_obj: Dict[LabwareTemplate, Location] = labware_location_hook(end_map)

        method_template = self._system.get_method_template(method_name)
        executer = MethodExecutor(method_template,
                                 start_map_obj,
                                 end_map_obj,
                                 self._system,
                                 self._system)
        self._method_executor_registry[executer.id] = executer
        return executer.id
    
    async def run_method(self, method_id: str) -> None:
        executer = self._method_executor_registry[method_id]
        # TODO:  Add check to make sure system is initailized
        await self.initialize()
        await executer.start()
        

    def stop(self) -> None:
        self._system.stop_all_threads()


if __name__ == "__main__":
    logging.basicConfig(handlers=[logging.StreamHandler()], level=logging.DEBUG)
    config_file_path =  r"C:\Users\miike\source\repos\orca\orca-core\examples\smc_assay\smc_assay_example.orca.yml"
    workflow_name = "smc-assay"
    available_drivers_registry: str = "https://raw.githubusercontent.com/Cheshire-Labs/orca-extensions/refs/heads/main/drivers.json"
    installed_registry = InstalledDriverRegistry()
    driver_manager = DriverManager(
            installed_registry,
            DriverLoader(), 
            DriverInstaller(installed_registry), 
            RemoteAvailableDriverRegistry(available_drivers_registry, installed_registry))
    orca = OrcaCore(config_file_path, driver_manager)
    workflow_id = orca.create_workflow_instance(workflow_name)
    asyncio.run(orca.run_workflow(workflow_id))