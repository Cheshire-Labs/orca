from orca.scripting.scripting import ThreadScript
from orca.system.system import ISystem
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.labware_thread import LabwareThread

class Spawn384TipsScript(ThreadScript):
    def __init__(self, system: ISystem):
        super().__init__(system)
        self._num_384_tips_spawned = 0

    def thread_notify(self, event: str, thread: LabwareThread) -> None:
        if event == LabwareThreadStatus.CREATED.name.upper():
            if self._num_384_tips_spawned % 4 != 0:
                thread.start_location = self.system.get_location(thread.end_location.name)
            self._num_384_tips_spawned += 1


