from workflow_models.method import MethodStatus

from system import System
from workflow_models.workflow import WorkflowTemplate


class WorkflowExecuter:
    def __init__(self, system: System, workflow: WorkflowTemplate) -> None:
        self._workflow = workflow
        self._system = system

    def execute(self) -> None:
        self._workflow.set_status_queued()
        router = Router(self._system)
        router.set_workflow(self._workflow)
        action = router.get_next_action()
        while action is not None:
            action.execute()
            action = router.get_next_action()