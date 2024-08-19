from orca.system.thread_manager import IThreadManager
from orca.workflow_models.status_enums import MethodStatus
from orca.workflow_models.labware_thread import IMethodObserver, Method
from orca.workflow_models.workflow_templates import ThreadTemplate


class SpawnThreadAction(IMethodObserver):
    def __init__(self, 
                 thread_manager: IThreadManager, 
                 thread_template: ThreadTemplate) -> None:
        self._thread_manager: IThreadManager = thread_manager
        self._template: ThreadTemplate = thread_template
        self._has_executed: bool = False

    def execute(self) -> None:
        thread = self._thread_manager.create_thread_instance(self._template)
        self._has_executed = True

    def method_notify(self, event: str, method: Method) -> None:
        if self._has_executed:
            return
        if event == MethodStatus.IN_PROGRESS.name.upper():
            self._template.set_wrapped_method(method)
            self.execute()