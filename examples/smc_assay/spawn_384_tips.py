from scripting.scripting import ThreadScript
from system.system import ISystem
from workflow_models.status_enums import LabwareThreadStatus
from workflow_models.workflow import LabwareThread

class Spawn384TipsScript(ThreadScript):
    def __init__(self, system: ISystem):
        super().__init__(system)
        self._num_384_tips_spawned = 0

    def thread_notify(self, event: LabwareThreadStatus, thread: LabwareThread) -> None:
        if event == LabwareThreadStatus.CREATED:
            if self._num_384_tips_spawned % 4 != 0:
                thread._start_location = self.system.get_location("stacker-384-tips-end")
            self._num_384_tips_spawned += 1


