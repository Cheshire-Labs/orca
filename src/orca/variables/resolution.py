"""What a variable resolves to on an execution, and which layer answered."""

from dataclasses import dataclass
from enum import Enum

from orca.variables.errors import OptionValue


class VariableSource(str, Enum):
    """The layer a resolved value came from.

    ``SUBMISSION_DIVERGED`` appears only in the merged per-execution listing,
    which has one slot per name and so cannot show two submissions that hold
    different values; ``VariableService.explain`` gives the per-submission
    breakdown.
    """

    SUBMISSION = "submission"
    SUBMISSION_DIVERGED = "submission-diverged"
    EXECUTION = "execution"
    GLOBAL = "global"
    COMPUTED = "computed"
    GLOBAL_DEFAULT = "global-default"
    WORKFLOW_DEFAULT = "workflow-default"


@dataclass(frozen=True)
class VariableBinding:
    """A resolved value together with the layer that answered."""

    value: OptionValue
    source: VariableSource


@dataclass(frozen=True)
class SubmissionOverride:
    """A value one submission holds in its own partition."""

    submission_id: str
    value: OptionValue


@dataclass(frozen=True)
class VariableResolution:
    """What the threads of one execution resolve for a single variable name.

    ``value`` / ``source`` are what a thread whose submission holds no override
    gets; ``overrides`` are the submissions whose own partition outranks it.
    Both are None when no layer below the submission partitions holds the name.
    """

    name: str
    execution_id: str
    value: OptionValue | None
    source: VariableSource | None
    overrides: tuple[SubmissionOverride, ...]

    @property
    def shadowed(self) -> bool:
        """True when at least one submission resolves something else."""
        return bool(self.overrides)

    def for_submission(self, submission_id: str) -> VariableBinding | None:
        """What one submission's threads resolve, or None if no layer holds it."""
        for override in self.overrides:
            if override.submission_id == submission_id:
                return VariableBinding(override.value, VariableSource.SUBMISSION)
        if self.value is None or self.source is None:
            return None
        return VariableBinding(self.value, self.source)
