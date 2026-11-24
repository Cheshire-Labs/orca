from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
"""Wire models for the submission operations.

Apart from `submission.py` because the Operation classes there take
`ISystemRuntime`, and the CLI reads these models over HTTP without
ever wanting the engine.
"""

from orca.modes.sim_pacing import SIM_SPEEDUP_RANGE
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Self
from orca.operations.wire_models import LabwareGroupDTO, RunModeStr, SubmissionDTO
from orca.modes.run_modes import WorkflowRunMode
from orca.operations.state_models import UnsettledSubjectDTO
from orca.runtime.status_models import SubmissionSnapshot
from orca.variables.errors import OptionValue


class SubmitExecutionRequest(BaseModel):
    """One submission of a workflow. No `groups` submits it as one default group."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_name: str = Field(..., max_length=128)
    variables: dict[str, OptionValue] | None = Field(default=None)
    groups: list[LabwareGroupDTO] | None = Field(
        default=None,
        description=(
            "One LabwareGroup per parallel plate-source within this "
            "submission. Each group is `{id, members: [{thread_template_name, "
            "acquisition}], name?}` where `acquisition` is `{kind: "
            "'pool'|'barcode'|'location', ...}`. Labware declared "
            "`GroupSharing.SHARED_ACROSS_GROUPS` (e.g. a final plate "
            "collecting from N samples) coalesces into ONE instance "
            "per submission when `len(groups) > 1`; "
            "`GroupSharing.PER_GROUP` labware gets one instance per "
            "group. Omit or pass null for one default group."
        ),
    )
    batch_mode: Literal["STANDALONE", "JOIN_EXISTING"] = Field(
        default="STANDALONE",
        description=(
            "STANDALONE (default): this submission always boots a fresh "
            "execution; later submissions cannot join. JOIN_EXISTING: "
            "for SubmissionBatching.BATCHABLE labware, this submission's "
            "contributions join the same workflow's in-flight ACCEPTING "
            "execution if one exists. Use JOIN_EXISTING when you want a "
            "shared receiver (e.g. a final plate) to keep collecting "
            "from late-arriving submissions."
        ),
    )
    operator_id: str | None = Field(
        default=None,
        description=(
            "Optional operator identity stamped onto the submission for "
            "audit. Single-deployment auth carries no per-user identity, "
            "so this is operator-supplied and not validated."
        ),
    )
    deployment_profile: str | None = Field(
        default=None,
        description=(
            "Optional name of a registered deployment profile. The "
            "runtime resolves it once at submit time and applies its "
            "values via the variable store. Editing the profile in the "
            "registry afterwards does NOT affect the running execution."
        ),
    )
    run_mode: Literal["CHECK_RUN", "SIM", "LIVE"] = Field(
        ...,
        description=(
            "Submit-time selector for the workflow run mode. REQUIRED: "
            "every submission must declare a mode. The Pydantic validator rejects the request at body-"
            "parse time with the standard 422 envelope when omitted; "
            "the runtime ``RunModeRequiredError`` is the same contract "
            "at the engine layer. The resolved value is stamped on the "
            "returned response so the caller can confirm which mode "
            "this submission actually runs under. Choose from "
            "CHECK_RUN / SIM / LIVE."
        ),
    )
    sim_speedup: float | None = Field(
        default=None,
        ge=SIM_SPEEDUP_RANGE[0], le=SIM_SPEEDUP_RANGE[1],
        allow_inf_nan=False,
        strict=True,
        description=(
            "How many times faster than real time this simulated run goes, "
            "0.1 to 1000: 1 is real time, 100 a hundred times faster. Omit for "
            "the server's default, which each server sets (a local install uses 100). "
            "Only for SIM: a LIVE or CHECK_RUN submission that carries it "
            "is refused, and so is a JOIN_EXISTING one (change the execution's "
            "speed-up with PUT /api/executions/{execution_id}/sim-speedup instead)."
        ),
    )


class SubmitExecutionResponse(BaseModel):
    """Every `SubmissionDTO` field, plus `unsettled`.

    A superset, not a mirror. Reading it as one is what had three CLI callers
    parsing the submit response into `SubmissionDTO` and dropping the field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    execution_id: str
    workflow_name: str
    group_count: int
    status: str
    batch_mode: str
    submitted_at: str
    run_mode: WorkflowRunMode
    operator_id: str | None = None
    deployment_profile: str | None = None
    sim_speedup: float | None = None
    """The execution's sim speed-up: 1 is real time. Null for a LIVE run and for a simulated run that does not wait."""
    unsettled: list[UnsettledSubjectDTO] = Field(default_factory=list)
    """What nobody has settled, as of this submission.

    Reported, never refused. Committing a deck to a run is the one moment an
    operator is certainly paying attention, and the same list read from
    `unsettled` needs somebody to think of asking. Empty is the ordinary case.
    """

    @classmethod
    def from_snapshot(
        cls, snap: SubmissionSnapshot,
        unsettled: list[UnsettledSubjectDTO] | None = None,
        sim_speedup: float | None = None,
    ) -> Self:
        return cls(
            id=snap.id,
            execution_id=snap.execution_id,
            workflow_name=snap.workflow_name,
            group_count=snap.group_count,
            status=snap.status,
            batch_mode=snap.batch_mode,
            submitted_at=snap.submitted_at,
            run_mode=snap.run_mode,
            operator_id=snap.operator_id,
            deployment_profile=snap.deployment_profile,
            sim_speedup=sim_speedup,
            unsettled=list(unsettled or []),
        )


class _OccupiedSlotExtra(BaseModel):
    """One ``extras.occupied[]`` row on the
    ``START_LOCATION_OCCUPIED`` envelope."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    position_id: str
    existing_labware_id: str
    existing_labware_name: str | None
    existing_template_name: str | None
    source: str


class ListSubmissionsRequest(BaseModel):
    """Optional ``execution_id`` filter restricts results to a single
    execution. None (default) returns every known submission across
    every live execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str | None = None


class ListSubmissionsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    submissions: list[SubmissionDTO]


class GetSubmissionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    submission_id: str


class GetSubmissionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    submission: SubmissionDTO


class MethodExecutionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    workflow_name: str = Field(
        ...,
        pattern=r"^[a-z_][a-z0-9_]*$",
        max_length=64,
        description="Registered workflow that owns the method.",
    )
    method_name: str = Field(
        ...,
        pattern=r"^[a-z_][a-z0-9_]*$",
        max_length=64,
        description="Method template name within that workflow's bundle.",
    )
    labware_start: dict[str, str] = Field(
        ..., description="Map of labware-template-name to start location-name.",
    )
    labware_end: dict[str, str] = Field(
        ..., description="Map of labware-template-name to end location-name.",
    )
    variables: dict[str, OptionValue] = Field(default_factory=dict)
    deployment_profile: str | None = Field(
        default=None,
        description="Optional registered deployment profile, applied once at submit time as for a workflow submission.",
    )
    event_hooks: list[str] | None = Field(
        default=None,
        description=(
            "Which of the workflow's wf.on handlers the run fires, by event name. Omitted fires every "
            "handler; [] fires none. An event the workflow has no handler for is refused."
        ),
    )
    run_mode: RunModeStr = Field(
        ...,
        description=(
            "Required per-submission run mode: CHECK_RUN, SIM, or "
            "LIVE. Same selector as workflow submission. Under LIVE each device "
            "runs as its own sim or live setting. No deployment-level fallback."
        ),
    )
    sim_speedup: float | None = Field(
        default=None,
        ge=SIM_SPEEDUP_RANGE[0], le=SIM_SPEEDUP_RANGE[1],
        allow_inf_nan=False,
        strict=True,
        description=(
            "How many times faster than real time this simulated run goes, as for a workflow "
            "submission. Only for SIM."
        ),
    )


class MethodExecutionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    workflow_name: str
    status: str


SUBMIT_EXECUTION_ROUTE = Route(
    SubmitExecutionRequest,
    SubmitExecutionResponse,
    RestBinding(
        method="POST", path="/executions", tag=RouteTag.EXECUTIONS,
        drives_device=True,
    ),
)


LIST_SUBMISSIONS_ROUTE = Route(
    ListSubmissionsRequest,
    ListSubmissionsResponse,
    RestBinding(
        method="GET", path="/submissions", tag=RouteTag.SUBMISSIONS,
    ),
)


GET_SUBMISSION_ROUTE = Route(
    GetSubmissionRequest,
    GetSubmissionResponse,
    RestBinding(
        method="GET", path="/submissions/{submission_id}", tag=RouteTag.SUBMISSIONS,
    ),
)


SUBMIT_METHOD_EXECUTION_ROUTE = Route(
    MethodExecutionRequest,
    MethodExecutionResponse,
    RestBinding(
        method="POST", path="/method-executions", tag=RouteTag.EXECUTIONS,
        status_code=201,
        drives_device=True,
    ),
)
