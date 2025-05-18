from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from typing import Any, Dict, List, Set, Union
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.workflow_models.action_template import MethodActionTemplate
from orca.workflow_models.labware_thread import IMethodObserver, Method


from abc import ABC, abstractmethod

from orca.workflow_models.action_factory import MethodActionFactory


class IMethodTemplate(ABC):
    @abstractmethod
    def get_instance(self, labware_reg: ILabwareRegistry) -> Method:
        raise NotImplementedError("IMethodTemplate must implement get_instance method")


class MethodTemplate(IMethodTemplate):

    def __init__(self, name: str, actions: List[MethodActionTemplate] = [], options: Dict[str, Any] = {}):
        self._name = name
        self._actions: List[MethodActionTemplate] = actions
        self._options = options
        self._method_observers: List[IMethodObserver] = []

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

    def add_method_observer(self, observer: IMethodObserver) -> None:
        self._method_observers.append(observer)

    def get_instance(self, labware_reg: ILabwareRegistry) -> Method:
        method = Method(self.name)
        for action_template in self.actions:
            factory = MethodActionFactory(action_template, labware_reg)
            action = factory.create_instance()
            method.append_step(action)
            for o in self._method_observers:
                method.add_observer(o)

        return method


class JunctionMethodTemplate(IMethodTemplate):
    def __init__(self) -> None:
        self._method: Method | None = None

    def set_method(self, method: Method) -> None:
        self._method = method

    def get_instance(self, labware_reg: ILabwareRegistry) -> Method:
        if self._method is None:
            raise NotImplementedError("Method has not been set")
        return self._method