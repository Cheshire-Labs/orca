"""An unsaved-record reporter for tests that keeps what the writers hand it."""

from orca.runtime.unsaved_record import PauseRun, UnsavedRecord


class UnsavedRecords:
    def __init__(self) -> None:
        self.reported: list[tuple[UnsavedRecord, BaseException]] = []

    def report(self, unsaved: UnsavedRecord, error: BaseException) -> None:
        self.reported.append((unsaved, error))

    def add_run_pauser(self, pause: PauseRun) -> None:
        del pause

    def remove_run_pauser(self, pause: PauseRun) -> None:
        del pause
