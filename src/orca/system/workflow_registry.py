from typing import Dict
from orca.sdk.events.event_bus_interface import IEventBus
from orca.sdk.events.event_handlers import Spawn
from orca.sdk.events.event_handlers import Join
from orca.sdk.events.execution_context import ThreadExecutionContext, WorkflowExecutionContext
from orca.system.interfaces import IMethodRegistry, IWorkflowRegistry
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.action_factory import MethodActionFactory
from orca.workflow_models.labware_thread import Method
from orca.workflow_models.method_template import IMethodTemplate, JunctionMethodTemplate, MethodTemplate
from orca.workflow_models.workflow import Workflow
from orca.workflow_models.workflow_templates import WorkflowTemplate
from orca.system.thread_manager_interface import IThreadManager

class MethodFactory:
    def __init__(self, labware_reg: ILabwareRegistry, event_bus: IEventBus) -> None:
        self._labware_reg: ILabwareRegistry = labware_reg
        self._event_bus: IEventBus = event_bus

    def create_instance(self, template: IMethodTemplate, context: ThreadExecutionContext) -> Method:
        if isinstance(template, JunctionMethodTemplate):
            return template.method
        elif isinstance(template, MethodTemplate):
            method = Method(self._event_bus, template.name, context)
            for action_template in template.actions:
                factory = MethodActionFactory(action_template, self._labware_reg, self._event_bus)
                action = factory.create_instance()
                method.append_action(action)
            return method
        else:
            raise TypeError(f"Unknown method template type: {type(template)}")
        
class WorkflowFactory:
    def __init__(self, thread_manager: IThreadManager, labware_registry: ILabwareRegistry, event_bus: IEventBus, system_map: SystemMap) -> None:
        self._thread_manager: IThreadManager = thread_manager
        self._labware_registry: ILabwareRegistry = labware_registry
        self._event_bus: IEventBus = event_bus
        self._system_map: SystemMap = system_map

    def create_instance(self, template: WorkflowTemplate) -> Workflow:
        workflow = Workflow(template.name, self._thread_manager)
        for thread_template in template.start_thread_templates:
            thread = self._thread_manager.create_thread_instance(thread_template, WorkflowExecutionContext(workflow.id, workflow.name))

        for joint in template.joints:
            self._event_bus.subscribe("METHOD.IN_PROGRESS", Join(joint.parent_thread, joint.attaching_thread, joint.parent_method))

        for spawn in template.spawns:
            self._event_bus.subscribe("METHOD.IN_PROGRESS", Spawn(spawn.spawn_thread, spawn.parent_thread, spawn.parent_method))
        
        for event in template.event_hooks:
            self._event_bus.subscribe(event.event_name, event.handler)
        return workflow



class MethodRegistry(IMethodRegistry):
    def __init__(self, method_factory: MethodFactory) -> None:
        self._method_factory = method_factory
        self._methods: Dict[str, Method] = {}

    def get_method(self, id: str) -> Method:
        return self._methods[id]
    
    def add_method(self, method: Method) -> None:
        self._methods[method.id] = method

    def create_method_instance(self, template: MethodTemplate, context: ThreadExecutionContext) -> Method:
        method = self._method_factory.create_instance(template, context)
        self.add_method(method)
        return method
    
    def clear(self) -> None:
        self._methods.clear()


class WorkflowRegistry(IWorkflowRegistry):
    def __init__(self, workflow_factory: WorkflowFactory) -> None:
        self._workflow_factory = workflow_factory
        self._workflows: Dict[str, Workflow] = {}

    def get_workflow(self, id: str) -> Workflow:
        return self._workflows[id]
    
    def add_workflow(self, workflow: Workflow) -> None:
        self._workflows[workflow.id] = workflow
    
    def create_workflow_instance(self, template: WorkflowTemplate) -> Workflow:
        return self._workflow_factory.create_instance(template)
    
    def clear(self) -> None:
        self._workflows.clear()
