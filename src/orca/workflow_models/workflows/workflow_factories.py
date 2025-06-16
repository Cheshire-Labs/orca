from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction
from orca.workflow_models.interfaces import IMethod
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method import MethodInstance
from orca.workflow_models.method_template import IMethodTemplate, JunctionMethodInstance, JunctionMethodTemplate, MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate


class MethodActionFactory:

    def __init__(self, template: ActionTemplate) -> None:
        self._template: ActionTemplate = template

    def create_instance(self) -> UnresolvedLocationAction:

        instance = UnresolvedLocationAction(self._template.resource_pool,
                                        self._template.command,
                                        self._template.inputs,
                                        self._template.outputs,
                                        self._template.options,
                                        )
        return instance


class MethodFactory:

    def create_instance(self, template: IMethodTemplate) -> IMethod:
        if isinstance(template, JunctionMethodTemplate):
            return JunctionMethodInstance(template.method)
        elif isinstance(template, MethodTemplate):
            method = MethodInstance( template.name)
            for action_template in template.actions:
                factory = MethodActionFactory(action_template)
                action = factory.create_instance()
                method.append_action(action)
            return method
        else:
            raise TypeError(f"Unknown method template type: {type(template)}")


class ThreadFactory:
    def __init__(self,
                 method_factory: MethodFactory) -> None:
        self._method_factory = method_factory

    def create_instance(self, template: ThreadTemplate) -> LabwareThreadInstance:

        # Instantiate labware
        labware_instance = template.labware_template.create_instance()

        # create the thread
        thread = LabwareThreadInstance(labware_instance,
                                template.start_location,
                                template.end_location,
                                )
        
        for method_template in template.method_resolvers:
            method = self._method_factory.create_instance(method_template)
            method.assign_thread(template.labware_template, thread)
            thread.append_method_sequence(method)

        return thread


class WorkflowFactory:
    def __init__(self, thread_factory: ThreadFactory) -> None:
        self._thread_factory = thread_factory

    def create_instance(self, template: WorkflowTemplate) -> WorkflowInstance:
        workflow = WorkflowInstance(template.name)
        for start_thread_template in template.entry_thread_templates:
            start_thread = self._thread_factory.create_instance(start_thread_template)
            workflow.add_entry_thread(start_thread)

        for spawn in template.spawns:
            workflow.add_spawn(spawn)

        for event in template.event_hooks:
            workflow.add_event_hook(event)
        return workflow
    


