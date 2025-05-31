from orca.workflow_models.action_template import MethodActionTemplate
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction


class MethodActionFactory:

    def __init__(self, template: MethodActionTemplate) -> None:
        self._template: MethodActionTemplate = template

    def create_instance(self) -> UnresolvedLocationAction:

        instance = UnresolvedLocationAction(self._template.resource_pool,
                                        self._template.command,
                                        self._template.inputs,
                                        self._template.outputs,
                                        self._template.options,
                                        )
        return instance