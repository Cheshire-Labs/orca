from collections.abc import Awaitable
from typing import Callable, Dict, List, Optional

from orca.workflow_models.action_context import ActionContext

ActionFunc = Callable[[ActionContext], Awaitable[None]]

from abc import ABC, abstractmethod
from orca.resource_models.devices import Device
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.variables.errors import OptionValue
from orca.state.records import DeclaredTracking
from orca.resource_models.well_selector import WellSelector
from orca.workflow_models.actions.location_action import ActionBodyLocationAction
from orca.workflow_models.status_enums import FailurePolicy


class ActionTemplate(ABC):
    """Base template for an action. An action is a single device operation
    performed on labware within a reserved device location."""

    def __init__(self,
                 operation_name: str,
                 resource: Device | ResourcePool,
                 inputs: List[LabwareTemplate | AnyLabwareTemplate],
                 outputs: Optional[List[LabwareTemplate | AnyLabwareTemplate]] = None,
                 options: Optional[Dict[str, OptionValue]] = None,
                 well_selectors: Optional[Dict[str, WellSelector]] = None,
                 declares: Optional[DeclaredTracking] = None) -> None:
        self._resource_pool: ResourcePool
        if isinstance(resource, Device):
            self._resource_pool = ResourcePool(resource.name, [resource])
        elif isinstance(resource, ResourcePool):
            self._resource_pool = resource
        else:
            raise TypeError("resource must be a Device or ResourcePool")
        self._operation_name = operation_name
        self._options: Dict[str, OptionValue] = {} if options is None else options
        self._inputs = inputs
        self._outputs = outputs if outputs is not None else inputs
        self._failure_policy = FailurePolicy.PAUSE
        self._tag: str | None = None
        self._deck_positions: Dict[LabwareTemplate, str] = {}
        self._well_selectors: Dict[str, WellSelector] = well_selectors or {}
        self._declares = declares
        self._injected_source: str | None = None

    @property
    def tag(self) -> str | None:
        return self._tag

    @tag.setter
    def tag(self, value: str) -> None:
        self._tag = value

    @property
    def injected_source(self) -> str | None:
        """The source `compile_action_code` compiled this from, or None for
        an action authored in a workflow file. Set post-construction --
        `compile_action_code` does not call this class's constructor
        directly, the injected source's own `@orca.action` decoration does.
        """
        return self._injected_source

    @injected_source.setter
    def injected_source(self, value: str) -> None:
        self._injected_source = value

    def to_dict(self) -> Dict[str, str | None]:
        """JSON-safe self-projection for the `@dangerous` audit trail.

        Without this, `_to_json_safe` falls back to a bare repr, and an
        injected action's actual source -- the thing that ran against real
        hardware -- is unrecoverable from the audit log afterwards.
        """
        return {"name": self.name, "injected_source": self._injected_source}

    @property
    def failure_policy(self) -> FailurePolicy:
        return self._failure_policy

    @failure_policy.setter
    def failure_policy(self, value: FailurePolicy) -> None:
        self._failure_policy = value

    @property
    def resource_pool(self) -> ResourcePool:
        return self._resource_pool

    @property
    def inputs(self) -> List[LabwareTemplate | AnyLabwareTemplate]:
        return self._inputs

    @property
    def outputs(self) -> List[LabwareTemplate | AnyLabwareTemplate]:
        return self._outputs

    @property
    def name(self) -> str:
        return self._operation_name

    @property
    def operation_name(self) -> str:
        return self._operation_name

    @property
    def options(self) -> Dict[str, OptionValue]:
        return self._options

    @property
    def deck_positions(self) -> Dict[LabwareTemplate, str]:
        return self._deck_positions

    @property
    def well_selectors(self) -> Dict[str, WellSelector]:
        return self._well_selectors

    @property
    def declares(self) -> DeclaredTracking | None:
        return self._declares

    @abstractmethod
    def get_location_action(self) -> ActionBodyLocationAction:
        raise NotImplementedError


class Action(ActionTemplate):
    """Code-first action created by @orca.action decorator.

    The function body contains device calls (shake, seal, aspirate, etc.)
    that execute within a single device reservation via the DeviceHandle
    queue bridge.
    """

    def __init__(
        self,
        func: ActionFunc,
        resource: Device | ResourcePool,
        inputs: List[LabwareTemplate | AnyLabwareTemplate],
        outputs: Optional[List[LabwareTemplate | AnyLabwareTemplate]] = None,
        options: Optional[Dict[str, OptionValue]] = None,
        failure_policy: Optional[FailurePolicy] = None,
        tag: str | None = None,
        deck_positions: Optional[Dict[LabwareTemplate, str]] = None,
        well_selectors: Optional[Dict[str, WellSelector]] = None,
        declares: Optional[DeclaredTracking] = None,
    ) -> None:
        self._func = func
        super().__init__(func.__name__, resource, inputs, outputs, options, well_selectors, declares)
        if failure_policy is not None:
            self._failure_policy = failure_policy
        if tag is not None:
            self._tag = tag
        if deck_positions is not None:
            self._deck_positions = deck_positions

    @property
    def func(self) -> ActionFunc:
        return self._func

    def get_location_action(self) -> ActionBodyLocationAction:
        return ActionBodyLocationAction(
            func=self._func,
            command=self._func.__name__,
        )
