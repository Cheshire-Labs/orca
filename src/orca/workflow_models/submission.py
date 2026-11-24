"""Submission: the envelope for handing labware groups to a running SystemRuntime.

A Submission carries one or more LabwareGroups, per-submission variable
overrides, operator/deployment audit fields, and an operator BatchMode
preference. Recorded verbatim on the runtime's submission manager.
"""

from dataclasses import dataclass, field
from datetime import datetime

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.workflow_models.labware_group import LabwareGroup
from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.submission_modes import BatchMode, SubmissionStatus
from orca.variables.errors import OptionValue


@dataclass(frozen=True)
class ResolvedAcquisition:
    """Submit-time resolution of a LabwareGroupMember's Acquisition.

    Populated by SystemRuntime._resolve_acquisitions OR the auto-spawn
    reuse-bind path. Consumed by the ThreadFactory when building entry
    threads / receivers.

    - labware_instance: for BarcodeAcquisition, the existing LabwareInstance
      fetched from the labware service. For reuse-bind, either the existing
      labware at the location or a newly-created instance. Factory attaches
      this instead of creating fresh from the template's pool.
    - start_location: for LocationAcquisition, the Location object resolved
      from the member's source_location string. Overrides the thread
      template's default start_location for this group's entry thread.
    - created_fresh: reuse-bind sets True when it freshly created the
      labware (so the factory seeds initial state). BarcodeAcquisition
      always leaves this False; the store-loaded instance has its own
      history already.

    All fields default => PoolAcquisition (pool-sourced at thread start).
    """
    labware_instance: LabwareInstance | None = None
    start_location: Location | None = None
    created_fresh: bool = False


@dataclass
class Submission:
    """An accepted or pending submission of labware groups to an execution.

    ``run_mode`` is the ``WorkflowRunMode`` this submission asked for.
    Stamped at submission acceptance so operators
    can confirm via REST that "this submission ran in CHECK_RUN" without
    re-deriving from the deployment defaults. Required field; the
    runtime always populates it.
    """
    id: str
    execution_id: str
    workflow_name: str
    groups: tuple[LabwareGroup, ...]
    variables: dict[str, OptionValue]
    batch_mode: BatchMode
    submitted_at: datetime
    run_mode: WorkflowRunMode
    operator_id: str | None = None
    deployment_profile: str | None = None
    status: SubmissionStatus = SubmissionStatus.PENDING
    # Populated by SystemRuntime._resolve_acquisitions at submit time; keyed
    # by (group_id, thread_template_name). Default empty for groupless /
    # pool-only submissions.
    resolved_acquisitions: dict[tuple[str, str], ResolvedAcquisition] = field(default_factory=dict)
