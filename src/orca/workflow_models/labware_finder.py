"""Labware finder hook for customizing partner selection during auto-spawn."""

from typing import Protocol

from orca.resource_models.labware_state import (
    FindResult,
    ILabwareRegistry,
)
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.thread_template import ThreadTemplate


class ILabwareFinderHook(Protocol):
    def find(self,
             template: ThreadTemplate,
             shared_method: ExecutingMethod,
             constraints: dict[str, str] | None,
             registry: ILabwareRegistry) -> FindResult: ...


class DefaultLabwareFinderHook:
    """Default finder: match template name, optional barcode filter, FIFO."""

    def find(self,
             template: ThreadTemplate,
             shared_method: ExecutingMethod,
             constraints: dict[str, str] | None,
             registry: ILabwareRegistry) -> FindResult:
        return registry.find_and_claim(template.labware_template.name, constraints)
