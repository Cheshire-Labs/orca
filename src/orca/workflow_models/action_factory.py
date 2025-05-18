from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.workflow_models.action_template import MethodActionTemplate
from orca.workflow_models.dynamic_resource_action import DynamicResourceAction


class MethodActionFactory:

    def __init__(self, template: MethodActionTemplate, labware_reg: ILabwareRegistry) -> None:
        self._template: MethodActionTemplate = template
        self._labware_reg = labware_reg

    def create_instance(self) -> DynamicResourceAction:

        # TODO: since refactoring, this MethodActionTemplate and DynamicResourceAction are very similar

        instance = DynamicResourceAction(self._labware_reg,
                                        self._template.resource_pool,
                                        self._template.command,
                                        self._template.inputs,
                                        self._template.outputs,
                                        self._template.options,
                                        )
        return instance