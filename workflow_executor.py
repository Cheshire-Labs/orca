from system.labware_registry_interfaces import ILabwareRegistry
from system.registry_interfaces import IThreadManager, IThreadRegistry
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
        
        self._thread_manager.execute_all_threads()

        
