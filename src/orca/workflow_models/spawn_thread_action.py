from orca.system.thread_manager_interface import IThreadManager
from orca.workflow_models.method import MethodInstance
from orca.workflow_models.status_enums import MethodStatus
from orca.workflow_models.labware_threads.labware_thread import IMethodObserver
from orca.workflow_models.thread_template import ThreadTemplate


class SpawnThreadAction(IMethodObserver):
    def __init__(self, 
                 thread_manager: IThreadManager, 
                 thread_template: ThreadTemplate) -> None:
        self._thread_manager: IThreadManager = thread_manager
        self._template: ThreadTemplate = thread_template
        self._has_executed: bool = False

    def execute(self) -> None:
        thread = self._thread_manager.start_labware_thread(self._template)
        self._has_executed = True

    def method_notify(self, event: str, method: MethodInstance) -> None:
        if self._has_executed:
            return
        if event == MethodStatus.IN_PROGRESS.name.upper():
            self._template.set_wrapped_method(method)
            self.execute()