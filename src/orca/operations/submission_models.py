"""Wire models for the submission operations.

Apart from `submission.py` because the Operation classes there take
`ISystemRuntime`, and the CLI reads these models over HTTP without
ever wanting the engine.
"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Self
from orca.daemon.schemas import LabwareGroupDTO, SubmissionDTO
from orca.runtime.run_modes import WorkflowRunMode
from orca.operations.state_models import UnsettledSubjectDTO
from orca.runtime.status_models import SubmissionSnapshot
from orca.variables.errors import OptionValue


class SubmitExecutionRequest(BaseModel):
    """Unified wire shape across all three surfaces.

    Identical to a hosted deployment's pre-Phase-2 `SubmitExecutionRequest` and a
    superset of orca-core's pre-Phase-2 `SubmissionSubmitRequest` —
    `groups` defaults to None (legacy single-group path), and absent
    `operator_id` / `deployment_profile` / non-default `batch_mode`
    routes through the legacy `submit_workflow` codepath.
    """

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
            "group. Omit or pass null for the legacy single-group "
            "default."
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
    run_mode: Literal["PURE_SIM", "DEVICE_SIM", "LIVE"] = Field(
        ...,
        description=(
            "Submit-time selector for the workflow run mode. REQUIRED: "
            "every submission must declare a mode. The Pydantic validator rejects the request at body-"
            "parse time with the standard 422 envelope when omitted; "
            "the runtime ``RunModeRequiredError`` is the same contract "
            "at the engine layer. The resolved value is stamped on the "
            "returned response so the caller can confirm which mode "
            "this submission actually runs under. Choose from "
            "PURE_SIM / DEVICE_SIM / LIVE."
        ),
    )
    acknowledge_warnings: bool = Field(
        default=False,
        description=(
            "Bypasses the LIVE-with-sim-overrides gate added in "
            "the run mode. When a LIVE submission references "
            "devices whose topology declares ``sim_override``, the "
            "runtime raises "
            "``LiveSubmissionWithSimOverridesUnacknowledgedError`` "
            "unless this flag is set. CLI surfaces should map "
            "``--confirm`` to this field."
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
    ) -> Self:
        return cls(
            id=snap.id,
            execution_id=snap.execution_id,
            workflow_name=snap.workflow_name,
            group_count=snap.group_count,
            status=snap.status.value if hasattr(snap.status, "value") else str(snap.status),
            batch_mode=snap.batch_mode.value if hasattr(snap.batch_mode, "value") else str(snap.batch_mode),
            submitted_at=snap.submitted_at,
            run_mode=snap.run_mode,
            operator_id=snap.operator_id,
            deployment_profile=snap.deployment_profile,
            unsettled=list(unsettled or []),
        )


class _LiveSimOverrideDeviceExtra(BaseModel):
    """One ``extras.devices[]`` row on the
    ``LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED`` envelope."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    sim_override: str
    resolved_mode: str


class _OccupiedSlotExtra(BaseModel):
    """One ``extras.occupied[]`` row on the
    ``START_LOCATION_OCCUPIED`` envelope."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    position_id: str
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
