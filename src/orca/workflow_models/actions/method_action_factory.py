from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction


class MethodActionFactory:

    def __init__(self, template: ActionTemplate) -> None:
        self._template = template

    def create_instance(self) -> UnresolvedLocationAction:

        instance = UnresolvedLocationAction(self._template.resource_pool,
                                        self._template.get_location_action(),
                                        self._template.inputs,
                                        self._template.outputs,
                                        self._template.options,
                                        self._template.failure_policy,
                                        self._template.deck_positions,
                                        self._template.well_selectors,
                                        self._template.declares,
                                        tag=self._template.tag,
                                        )
        return instance
