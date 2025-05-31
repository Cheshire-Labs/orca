from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.method import Method
from orca.workflow_models.method_template import JunctionMethodTemplate


from typing import List


class ThreadTemplate:

    def __init__(self, 
                 labware_template: LabwareTemplate, 
                 start: Location, 
                 end: Location, 
                 methods: List[IMethodTemplate] = [],                  
                 ) -> None:
        self._labware_template: LabwareTemplate = labware_template
        self._start: Location = start
        self._end: Location = end
        self._methods: List[IMethodTemplate] = methods
        self._wrapped_method: Method | None = None
        
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


    def set_wrapped_method(self, wrapped_method: Method) -> None:
        self._wrapped_method
        for m in self._methods:
            if isinstance(m, JunctionMethodTemplate):
                m.set_method(wrapped_method)

    def add_method(self, method: IMethodTemplate) -> None:
        self._methods.append(method)

    def add_methods(self, methods: List[IMethodTemplate]) -> None:
        for method in methods:
            self.add_method(method)