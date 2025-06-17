from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.method_template import JunctionMethodTemplate


from typing import List, Optional


class ThreadTemplate:
    """ Creates a template for a thread.  A thread is a sequence of methods to be operated on a specific labware. """
    def __init__(self, 
                 labware_template: LabwareTemplate, 
                 start: Location, 
                 end: Location, 
                 methods: Optional[List[IMethodTemplate]] = None,                  
                 ) -> None:
        """ Initializes a ThreadTemplate instance.
        Args:
            labware_template (LabwareTemplate): The labware template that this thread will operate on.
            start (Location): The starting location of the thread.
            end (Location): The ending location of the thread.
            methods (Optional[List[IMethodTemplate]], optional): A list of method templates that define the methods in the thread. Defaults to None.
        """
        self._labware_template: LabwareTemplate = labware_template
        self._start: Location = start
        self._end: Location = end
        self._methods: List[IMethodTemplate] = methods if methods is not None else []
        
    @property
    def name(self) -> str:
        return self._labware_template.name

    @property
    def labware_template(self) -> LabwareTemplate:
        return self._labware_template

    @property
    def start_location(self) -> Location:
        return self._start

    @property
    def end_location(self) -> Location:
        return self._end

    @property
    def method_resolvers(self) -> List[IMethodTemplate]:
        return self._methods

    def set_wrapped_method(self, wrapped_method: ExecutingMethod) -> None:
        if not any([isinstance(m, JunctionMethodTemplate) for m in self._methods]):
            raise ValueError(f"No wrapper methods found to wrap {wrapped_method.name} within thread {self.name}")
        for m in self._methods:
            if isinstance(m, JunctionMethodTemplate):
                m.set_method(wrapped_method)

    def add_method(self, method: IMethodTemplate) -> None:
        self._methods.append(method)

    def add_methods(self, methods: List[IMethodTemplate]) -> None:
        for method in methods:
            self.add_method(method)