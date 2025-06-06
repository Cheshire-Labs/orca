from orca.scripting.scripting import ThreadScript
from orca.system.system_interface import ISystem
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance


class SpawnFinalPlateScript(ThreadScript):
    def __init__(self, system: ISystem):
        super().__init__(system)
        self._transfer_count = 0

    def thread_notify(self, event: str, thread: LabwareThreadInstance) -> None:
        if event == LabwareThreadStatus.CREATED.name.upper():
            if self._transfer_count % 4 != 0:
                thread.start_location = self.system.get_location(thread.end_location.name)
            self._transfer_count += 1