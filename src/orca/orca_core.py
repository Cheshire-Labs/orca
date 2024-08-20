import os
from typing import Any, Callable, Coroutine, Dict, List, Optional
import logging
import asyncio
from orca.helper import FilepathReconciler
from orca.scripting.scripting import IScriptRegistry
from orca.system.method_executor import MethodExecutor
from orca.resource_models.base_resource import IInitializableResource
from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.system.system import ISystem
from orca.yml_config_builder.config_file import ConfigFile
from orca.yml_config_builder.template_factories import ConfigToSystemBuilder
from orca.yml_config_builder.resource_factory import IResourceFactory, ResourceFactory




class OrcaCore:
    def __init__(self, 
                 config_filepath: str, 
                 options: Dict[str, Any] = {},
                 scripting_registry: Optional[IScriptRegistry] = None,
                 resource_factory: Optional[IResourceFactory] = None) -> None:
        self._config = ConfigFile(config_filepath)
        self._config.set_command_line_options(options)
        builder = ConfigToSystemBuilder()
        if scripting_registry is not None:
            builder.set_script_registry(scripting_registry)
        if resource_factory is None:
            absolute_path = os.path.abspath(config_filepath)
            directory_path = os.path.dirname(absolute_path)
            filepath_reconciler = FilepathReconciler(directory_path)
            resource_factory = ResourceFactory(filepath_reconciler)
        builder.set_resource_factory(resource_factory)
        self._system: ISystem = self._config.get_system(builder)

    @property
    def system(self) -> ISystem:
        return self._system

    async def initialize(self,
                   resource_list: Optional[List[str]] = None,
                   options: Dict[str, Any] = {}) -> None:
        if resource_list is None:
            await self._system.initialize_all()
        else:
            init_fxns: List[Callable[[], Coroutine[Any, Any, None]]] = []
            for resource_name in resource_list:
                resource = self._system.get_resource(resource_name)
                if isinstance(resource, IInitializableResource):
                    init_fxns.append(resource.initialize)
            await asyncio.gather(*[f() for f in init_fxns])

    async def run_workflow(self, workflow_name: str) -> str:
        workflow_template = self._system.get_workflow_template(workflow_name)
        workflow = self._system.create_workflow_instance(workflow_template)

        # TODO:  Add check to make sure system is initailized
        await self.initialize()
        await workflow.start()
        return workflow.id
        # executer = WorkflowExecuter(workflow_template,
        #                             self._system,
        #                             self._system,
        #                             self._system)
        # self.initialize()
        # executer.execute()

    async def run_method(self, method_name: str, start_map: Dict[str, str], end_map: Dict[str, str]) -> str:
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
        await self.initialize()
        await executer.start()
        return executer.id


if __name__ == "__main__":
    logging.basicConfig(handlers=[logging.StreamHandler()], level=logging.DEBUG)
    config_file_path =  r"C:\Users\miike\source\repos\orca\orca-core\examples\smc_assay\smc_assay_example.yml"
    workflow_name = "smc-assay"
    with open(config_file_path, "r") as f:
        yml_content = f.read()
    orca = OrcaCore(yml_content)
    # asyncio.run(orca.initialize())
    asyncio.run(orca.run_workflow(workflow_name))