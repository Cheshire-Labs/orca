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
                 thread_registry: IThreadRegistry) -> None:
        self._template = workflow_template
        self._labware_registry = labware_registry
        self._thread_registry = thread_registry

    def execute(self) -> None:
        thread_manager: IThreadManager = ThreadManager()



        for thread_template in self._template.start_thread_templates:
            start_thread = self._thread_registry.create_thread_instance(thread_template)
            start_thread.initialize_labware()
            thread_manager.add_thread(start_thread) # TODO: combine with Registry?
            for method_template in thread_template.method_resolvers:
                method = method_template.get_instance(self._labware_registry)
                
                start_thread.append_method_sequence(method)


        
        thread_manager.execute()

        
