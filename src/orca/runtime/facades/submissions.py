"""SubmissionFacade: T6 LabwareGroup / Submission API.

Delegates to SystemRuntime.submit() for the underlying batching behavior
and reads back Submission records the runtime stashes on each Execution.
Exposes four operations:

  submit_group     : accept one submission (a workflow + groups).
  list_submissions : list all known submissions (optionally per-execution).
  get_submission   : fetch one submission by id.
  close_execution  : transition an execution from ACCEPTING to DRAINING so
                     JOIN_EXISTING submissions are rejected; STANDALONE
                     submissions for the same workflow start fresh.

`close_execution` operates on the execution container, not a submission:
operators submit work over time (same-execution batching) and then signal
"no more coming" on the execution. The execution drains its in-flight work
and terminates.
"""

from typing import Iterator, Protocol, Sequence

from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.execution import Execution
from orca.runtime.execution_phase import ExecutionPhase
from orca.runtime.labware_group import LabwareGroup
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import ISubmissionFacade
from orca.runtime.status_models import SubmissionCloseResult, SubmissionSnapshot
from orca.runtime.submission import Submission
from orca.runtime.submission_modes import BatchMode
from orca.system.system_interface import ISystem
from orca.variables.errors import OptionValue
from orca.workflow_models.workflow_templates import WorkflowTemplate


class ISubmissionRuntimeAccess(Protocol):
    """Narrow surface of SystemRuntime that SubmissionFacade needs.

    Avoids a circular import between submissions.py and system_runtime.py.
    Satisfied structurally by SystemRuntime.
    """

    @property
    def system(self) -> ISystem: ...

    async def submit(
        self,
        workflow: WorkflowTemplate,
        groups: Sequence[LabwareGroup] = (),
        variables: dict[str, OptionValue] | None = None,
        batch_mode: BatchMode = BatchMode.STANDALONE,
        operator_id: str | None = None,
        deployment_profile: str | None = None,
        mode: WorkflowRunMode | None = None,
        acknowledge_warnings: bool = False,
    ) -> Submission: ...

    def close_execution(self, execution_id: str) -> ExecutionPhase: ...

    def iter_executions(self) -> Iterator[Execution]: ...

    def require_execution(self, execution_id: str) -> Execution: ...


class SubmissionFacade(ISubmissionFacade):
    """Concrete SubmissionFacade. Owned by `SystemRuntime`.

    Holds a narrow runtime-access surface so the facade can submit new
    groups, finalize batches, and iterate submissions across executions
    without importing SystemRuntime directly.
    """

    def __init__(self, runtime: ISubmissionRuntimeAccess) -> None:
        self._runtime = runtime

    # -- Writes --------------------------------------------------------------

    @dangerous(
        name="submission.submit_group",
        level=DangerLevel.OPERATOR,
        message="Submit labware group(s) to workflow '{workflow_name}' with "
                "batch_mode={batch_mode}. JOIN_EXISTING may merge into a live "
                "batch receiving submissions; STANDALONE always boots a fresh "
                "execution.",
    )
    async def submit_group(
        self, *,
        workflow_name: str,
        groups: tuple[LabwareGroup, ...] = (),
        variables: dict[str, OptionValue] | None = None,
        batch_mode: BatchMode = BatchMode.STANDALONE,
        operator_id: str | None = None,
        deployment_profile: str | None = None,
        mode: WorkflowRunMode | None = None,
        acknowledge_warnings: bool = False,
    ) -> SubmissionSnapshot:
        template = self._runtime.system.get_workflow_template(workflow_name)
        submission = await self._runtime.submit(
            template,
            groups=groups,
            variables=dict(variables) if variables else None,
            batch_mode=batch_mode,
            operator_id=operator_id,
            deployment_profile=deployment_profile,
            mode=mode,
            acknowledge_warnings=acknowledge_warnings,
        )
        return _snapshot(submission)

    @dangerous(
        name="execution.close",
        level=DangerLevel.OPERATOR,
        message="Close execution '{execution_id}'. JOIN_EXISTING submissions "
                "for this execution will be rejected. STANDALONE submissions "
                "for the same workflow start a fresh execution. Live threads "
                "in this execution continue to completion.",
    )
    def close_execution(
        self, execution_id: str,
    ) -> SubmissionCloseResult:
        phase = self._runtime.close_execution(execution_id)
        return SubmissionCloseResult(
            execution_id=execution_id,
            phase=phase,
        )

    # -- Reads ---------------------------------------------------------------

    def list_submissions(
        self, *, execution_id: str | None = None,
    ) -> list[SubmissionSnapshot]:
        if execution_id is not None:
            execution = self._runtime.require_execution(execution_id)
            return [_snapshot(s) for s in execution.submissions]
        result: list[SubmissionSnapshot] = []
        for execution in self._runtime.iter_executions():
            for submission in execution.submissions:
                result.append(_snapshot(submission))
        return result

    def get_submission(self, submission_id: str) -> SubmissionSnapshot:
        return _snapshot(self._require_submission(submission_id))

    # -- Internals -----------------------------------------------------------

    def _require_submission(self, submission_id: str) -> Submission:
        for execution in self._runtime.iter_executions():
            for submission in execution.submissions:
                if submission.id == submission_id:
                    return submission
        raise KeyError(f"No submission with id '{submission_id}'")


def _snapshot(submission: Submission) -> SubmissionSnapshot:
    return SubmissionSnapshot(
        id=submission.id,
        execution_id=submission.execution_id,
        workflow_name=submission.workflow_name,
        group_count=len(submission.groups),
        status=submission.status,
        batch_mode=submission.batch_mode,
        submitted_at=submission.submitted_at.isoformat(),
        run_mode=submission.run_mode,
        operator_id=submission.operator_id,
        deployment_profile=submission.deployment_profile,
    )
