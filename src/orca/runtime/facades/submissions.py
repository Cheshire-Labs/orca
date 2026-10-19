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

from typing import Iterator, Mapping, Protocol, Sequence

from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.execution import Execution, StopOutcome
from orca.runtime.execution_record import ExecutionRecord
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
        sim_time_scale: float | None = None,
    ) -> Submission: ...

    def close_execution(self, execution_id: str) -> ExecutionPhase: ...

    async def submit_workflow(
        self,
        workflow_name: str,
        variables: Mapping[str, OptionValue] | None = None,
        deployment_profile: str | None = None,
        *,
        mode: WorkflowRunMode | None = None,
        acknowledge_warnings: bool = False,
        sim_time_scale: float | None = None,
    ) -> ExecutionRecord: ...

    async def stop_execution(
        self, execution_id: str, *, confirm: bool = False,
    ) -> StopOutcome: ...

    def remove_execution(self, execution_id: str) -> None: ...

    async def confirm_manual_step(self, execution_id: str, step_id: str) -> None: ...

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
        sim_time_scale: float | None = None,
        reason: str | None = None,
    ) -> SubmissionSnapshot:
        del reason  # consumed by @dangerous audit
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
            sim_time_scale=sim_time_scale,
        )
        return _snapshot(submission)

    @dangerous(
        name="submission.submit_workflow",
        level=DangerLevel.OPERATOR,
        message="Submit workflow '{workflow_name}' as one fresh execution.",
    )
    async def submit_workflow(
        self,
        workflow_name: str,
        variables: Mapping[str, OptionValue] | None = None,
        *,
        mode: WorkflowRunMode | None = None,
        acknowledge_warnings: bool = False,
        sim_time_scale: float | None = None,
        reason: str | None = None,
    ) -> ExecutionRecord:
        del reason  # consumed by @dangerous audit
        return await self._runtime.submit_workflow(
            workflow_name, variables,
            mode=mode, acknowledge_warnings=acknowledge_warnings,
            sim_time_scale=sim_time_scale,
        )

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
        *, reason: str | None = None,
    ) -> SubmissionCloseResult:
        del reason  # consumed by @dangerous audit
        phase = self._runtime.close_execution(execution_id)
        return SubmissionCloseResult(
            execution_id=execution_id,
            phase=phase,
        )

    @dangerous(
        name="execution.stop",
        level=DangerLevel.CRITICAL,
        message="Stop execution '{execution_id}'. The first call pauses it and "
                "arms the abort; a second confirmed call on the armed, paused "
                "execution aborts it.",
    )
    async def stop_execution(
        self, execution_id: str, *, abort: bool = False, reason: str | None = None,
    ) -> StopOutcome:
        del reason  # consumed by @dangerous audit
        return await self._runtime.stop_execution(execution_id, confirm=abort)

    @dangerous(
        name="execution.remove",
        level=DangerLevel.OPERATOR,
        message="Remove finished execution '{execution_id}' and clear its events.",
    )
    def remove_execution(self, execution_id: str, *, reason: str | None = None) -> None:
        del reason  # consumed by @dangerous audit
        self._runtime.remove_execution(execution_id)

    @dangerous(
        name="execution.confirm_manual_step",
        level=DangerLevel.OPERATOR,
        message="Confirm manual step '{step_id}' in execution '{execution_id}'. "
                "The action waiting on it carries on.",
    )
    async def confirm_manual_step(
        self, execution_id: str, step_id: str, *, reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
        await self._runtime.confirm_manual_step(execution_id, step_id)

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
