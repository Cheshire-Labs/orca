from typing import Dict
from resource_models.labware import Labware
from system.labware_registry_interfaces import ILabwareRegistry
from system.registries import ThreadManager
from system.registry_interfaces import IThreadManager, IThreadRegistry, IWorkflowRegistry
from workflow_models.workflow_templates import WorkflowTemplate


class WorkflowExecuter:
    def __init__(self, 
                 workflow_template: WorkflowTemplate,
                 labware_registry: ILabwareRegistry,
                 thread_registry: IThreadRegistry, 
                 thread_manager: IThreadManager,) -> None:
        self._template = workflow_template
        self._labware_registry = labware_registry
        self._thread_registry = thread_registry
        self._thread_manager = thread_manager

    def execute(self) -> None:

        for thread_template in self._template.start_thread_templates:
            start_thread = self._thread_registry.create_thread_instance(thread_template)
            # start_thread.initialize_labware()
            # for method_template in thread_template.method_resolvers:
            #     method = method_template.get_instance(self._labware_registry)
                
            #     start_thread.append_method_sequence(method)
        
        self._thread_manager.execute()

        
