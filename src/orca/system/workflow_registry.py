from typing import Dict
from orca.sdk.events.event_bus_interface import IEventBus
from orca.sdk.events.event_handlers import Spawn
from orca.sdk.events.event_handlers import Join
from orca.system.interfaces import IWorkflowRegistry
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.labware_thread import Method
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.workflow import Workflow
from orca.workflow_models.workflow_templates import WorkflowTemplate
from orca.system.thread_manager_interface import IThreadManager


class WorkflowFactory:
    def __init__(self, thread_manager: IThreadManager, labware_registry: ILabwareRegistry, event_bus: IEventBus, system_map: SystemMap) -> None:
        self._thread_manager: IThreadManager = thread_manager
        self._labware_registry: ILabwareRegistry = labware_registry
        self._event_bus: IEventBus = event_bus
        self._system_map: SystemMap = system_map

    def create_instance(self, template: WorkflowTemplate) -> Workflow:
        workflow = Workflow(template.name, self._thread_manager)
        for thread_template in template.start_thread_templates:
            # thread = self._thread_manager.create_thread_instance(thread_template)  probably not needed
            workflow.add_start_thread(thread_template)

        for joint in template.joints:
            self._event_bus.subscribe("THREAD.START", Join(joint.parent_thread, joint.attaching_thread, joint.parent_method))

        for spawn in template.spawns:
            self._event_bus.subscribe("METHOD.START", Spawn(spawn.spawn_thread, spawn.parent_thread, spawn.parent_method))
        return workflow


class WorkflowRegistry(IWorkflowRegistry):
    def __init__(self, workflow_factory: WorkflowFactory) -> None:
        self._workflow_factory = workflow_factory
        self._workflows: Dict[str, Workflow] = {}
        self._methods: Dict[str, Method] = {}

    def get_workflow(self, id: str) -> Workflow:
        return self._workflows[id]
    
    def add_workflow(self, workflow: Workflow) -> None:
        if workflow.id in self._workflows.keys():
            raise KeyError(f"Workflow {workflow.id} is already defined in the system.  Each workflow must have a unique id")
        self._workflows[workflow.id] = workflow
    
    def create_workflow_instance(self, template: WorkflowTemplate) -> Workflow:
        return self._workflow_factory.create_instance(template)
    
    def get_method(self, id: str) -> Method:
        return self._methods[id]
    
    def add_method(self, method: Method) -> None:
        if method.id in self._methods.keys():
            raise KeyError(f"Method {method.id} is already defined in the system.  Each method must have a unique id")
        self._methods[method.id] = method

    def create_method_instance(self, template: MethodTemplate) -> Method:
        # TODO: May need to delete this method and rexamine the design for method access
        raise NotImplementedError
    
    def clear(self) -> None:
        self._workflows.clear()
        self._methods.clear()