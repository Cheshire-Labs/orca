"""What happens to a record a background writer could not save.

Run status, incidents, audit rows and events are saved in the background. Each
writer tries a failing save `WRITE_ATTEMPTS` times, then hands the record to
`UnsavedRecordReporter.report`. That one function decides what happens next, so
a rule that treats some errors differently has one place to go.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placement import PositionNotSavedError
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentDetail,
    IncidentSeverity,
    RecordNotSavedDetail,
    RecoveryAction,
    SystemIncident,
)

logger = logging.getLogger(__name__)

RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.5, 1.0)
WRITE_ATTEMPTS = 1 + len(RETRY_DELAYS_SECONDS)

PauseRun = Callable[[str, str], bool]
"""Pause the run with this id, giving this message. False when the caller does not hold that run, or it has ended."""


@dataclass(frozen=True)
class UnsavedRecord:
    kind: str
    record: str
    execution_id: str | None
    # True for the "could not save" incident and its event, so a failure to save those is not reported again.
    reports_an_unsaved_record: bool = False


class IUnsavedRecordReporter(Protocol):
    def report(self, unsaved: UnsavedRecord, error: BaseException) -> None: ...

    def add_run_pauser(self, pause: PauseRun) -> None: ...

    def remove_run_pauser(self, pause: PauseRun) -> None: ...


class RecordIncident(Protocol):
    def __call__(
        self,
        category: IncidentCategory,
        severity: IncidentSeverity,
        message: str,
        detail: IncidentDetail,
        recovery_action: RecoveryAction,
        execution_id: str | None = None,
        thread_id: str | None = None,
    ) -> SystemIncident: ...


class UnsavedRecordReporter:
    """Logs the record, pauses its run, and raises an incident. Call it on the event loop."""

    def __init__(self, record_incident: RecordIncident) -> None:
        self._record_incident = record_incident
        self._pausers: list[PauseRun] = []

    def add_run_pauser(self, pause: PauseRun) -> None:
        self._pausers.append(pause)

    def remove_run_pauser(self, pause: PauseRun) -> None:
        if pause in self._pausers:
            self._pausers.remove(pause)

    def report(self, unsaved: UnsavedRecord, error: BaseException) -> None:
        """What happens after a writer's last try. It never raises, so a writer that calls it keeps saving."""
        logger.error(
            "could not save %s (%s: %s); record: %s",
            unsaved.kind, type(error).__name__, error, unsaved.record,
            exc_info=(type(error), error, error.__traceback__),
        )
        if unsaved.reports_an_unsaved_record:
            return
        try:
            paused = unsaved.execution_id is not None and self._pause(unsaved.execution_id, _pause_message(unsaved, error))
            self._record_incident(
                category=IncidentCategory.RECORD_NOT_SAVED,
                severity=IncidentSeverity.ERROR,
                message=_incident_message(unsaved, error, paused),
                detail=RecordNotSavedDetail(
                    record_kind=unsaved.kind,
                    record=unsaved.record,
                    error_type=type(error).__name__,
                    error_message=str(error),
                ),
                recovery_action=RecoveryAction.RESUME_EXECUTION if paused else RecoveryAction.NONE,
                execution_id=unsaved.execution_id,
            )
        except Exception:
            logger.exception("could not report the unsaved %s", unsaved.kind)

    def _pause(self, execution_id: str, message: str) -> bool:
        return any(pause(execution_id, message) for pause in list(self._pausers))


def _cause(unsaved: UnsavedRecord, error: BaseException) -> str:
    return f"Orca could not save {unsaved.kind} to the database ({type(error).__name__}: {error})."


def _pause_message(unsaved: UnsavedRecord, error: BaseException) -> str:
    return f"{_cause(unsaved, error)} The run is paused so its record stays complete."


def _incident_message(unsaved: UnsavedRecord, error: BaseException, paused: bool) -> str:
    if paused:
        return (
            f"{_pause_message(unsaved, error)} Check the database, then resume the run or stop it. "
            "If the next save fails, the run pauses again."
        )
    if unsaved.execution_id is not None:
        return f"{_cause(unsaved, error)} Its run has ended or is not loaded, so nothing was paused. The record is missing."
    return f"{_cause(unsaved, error)} It belongs to no run, so nothing was paused. The record is missing."


class UnsavedPositions:
    """A labware position the placer could not save after its caller was cancelled, handed to the reporter."""

    def __init__(self, reporter: IUnsavedRecordReporter) -> None:
        self._reporter = reporter

    def report(self, labware: LabwareInstance, error: PositionNotSavedError) -> None:
        self._reporter.report(
            UnsavedRecord(kind="a labware position", record=f"{labware.name} ({labware.id})", execution_id=None),
            error,
        )
