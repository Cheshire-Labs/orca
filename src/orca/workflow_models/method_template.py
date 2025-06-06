from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from typing import Any, Dict, List, Set, Union
from orca.sdk.events.execution_context import WorkflowExecutionContext
from orca.workflow_models.action_template import MethodActionTemplate
from orca.workflow_models.method import MethodInstance


from abc import ABC, abstractmethod



class IMethodTemplate(ABC):
    pass

class MethodTemplate(IMethodTemplate):

    def __init__(self, name: str, actions: List[MethodActionTemplate] = [], options: Dict[str, Any] = {}):
        self._name = name
        self._actions: List[MethodActionTemplate] = actions
        self._options = options

    @property
    def name(self) -> str:
        return self._name

    @property
    def actions(self) -> List[MethodActionTemplate]:
        return self._actions


    @property
    def inputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        inputs: Set[Union[LabwareTemplate, AnyLabwareTemplate]] = set()
        for action in self._actions:
            inputs.update(action.inputs)
        return list(inputs)

    @property
    def outputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        outputs: Set[Union[LabwareTemplate, AnyLabwareTemplate]] = set()
        for action in self._actions:
            outputs.update(action.outputs)
        return list(outputs)

    def append_action(self, action: MethodActionTemplate):
        self._actions.append(action)

    def add_actions(self, actions: List[MethodActionTemplate]) -> None:
        for action in actions:
            self.append_action(action)

    # def get_instance(self, labware_reg: ILabwareRegistry, event_bus: IEventBus) -> Method:
    #     method = Method(event_bus, self._name)
    #     for action_template in self.actions:
    #         factory = MethodActionFactory(action_template, labware_reg, event_bus)
    #         action = factory.create_instance()
    #         method.append_step(action)

    #     return method


class JunctionMethodTemplate(IMethodTemplate):
    def __init__(self) -> None:
        self._method: MethodInstance | None = None

    @property
    def method(self) -> MethodInstance:
        if self._method is None:
            raise NotImplementedError("Method has not been set")
        return self._method

    def set_method(self, method: MethodInstance) -> None:
        self._method = method

    # def get_instance(self, labware_reg: ILabwareRegistry, event_bus: IEventBus) -> Method:
    #     if self._method is None:
    #         raise NotImplementedError("Method has not been set")
    #     return self._method




