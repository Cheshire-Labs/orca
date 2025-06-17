from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction
from orca.workflow_models.interfaces import ILabwareThread, IMethod
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method import ExecutingMethod, MethodInstance


from abc import ABC



class IMethodTemplate(ABC):
    pass

class MethodTemplate(IMethodTemplate):
    """ Creates a template for a method. A method is a sequence of actions"""
    def __init__(self, name: str, actions: Optional[List[ActionTemplate]] = None, options: Optional[Dict[str, Any]] = None):
        """ Initializes a MethodTemplate instance.
        Args:
            name (str): The name of the method template.
            actions (Optional[List[ActionTemplate]], optional): A list of action templates that define the actions in the method. Defaults to None.
            options (Optional[Dict[str, Any]], optional): A dictionary of options to configure the method. Defaults to None.
        """
        self._name = name
        self._actions: List[ActionTemplate] = actions if actions is not None else []
        self._options = options if options is not None else {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def actions(self) -> List[ActionTemplate]:
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

    def append_action(self, action: ActionTemplate):
        self._actions.append(action)

    def add_actions(self, actions: List[ActionTemplate]) -> None:
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
    """ Creates a template for a junction method. A junction method is a placeholder for a method that will be defined later in the workflow."""
    def __init__(self) -> None:
        self._method: ExecutingMethod | None = None
        self._thread_assignments: List[Tuple[LabwareTemplate, ILabwareThread]] = []
    

    @property
    def method(self) -> ExecutingMethod:
        if self._method is None:
            raise NotImplementedError("Method has not been set")
        return self._method

    def set_method(self, method: ExecutingMethod) -> None:
        self._method = method
        for input_template, thread in self._thread_assignments:
            method.assign_thread(input_template, thread)

    def assign_thread(self, input_template: LabwareTemplate, thread: ILabwareThread) -> None:
        if self._method:
            self._method.assign_thread(input_template, thread)
        else:
            self._thread_assignments.append((input_template, thread))

    # def get_instance(self, labware_reg: ILabwareRegistry, event_bus: IEventBus) -> Method:
    #     if self._method is None:
    #         raise NotImplementedError("Method has not been set")
    #     return self._method


class JunctionMethodInstance(IMethod):
    def __init__(self, method: ExecutingMethod) -> None:
        self._method = method

    @property
    def id(self) -> str:
        return self._method.id

    @property
    def name(self) -> str:
        return self._method.name
    
    @property
    def actions(self) -> List[UnresolvedLocationAction]:
        return self._method.actions

    def append_action(self, action: UnresolvedLocationAction) -> None:
        return self._method.append_action(action)

    def assign_thread(self, input_template: LabwareTemplate, thread: ILabwareThread) -> None:
        return self._method.assign_thread(input_template, thread)

    