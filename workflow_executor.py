from typing import Dict
from system.labware_registry_interfaces import ILabwareRegistry
from system.registry_interfaces import IThreadRegistry
from system.system_map import SystemMap
from workflow_models.workflow_templates import WorkflowTemplate



class WorkflowExecuter:
    def __init__(self, workflow: WorkflowTemplate, thread_reg: IThreadRegistry) -> None:
        self._template = workflow
        self._thread_registry = thread_reg

    def execute(self) -> None:
        workflow = self._thread_registry.create_thread_instance(self._template)
        workflow.execute()
