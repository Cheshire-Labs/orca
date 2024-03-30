from system.registry_interfaces import IThreadRegistry
from workflow_models.status_enums import MethodStatus
from workflow_models.workflow import IMethodObserver, Method
from workflow_models.workflow_templates import ThreadTemplate


class SpawnThreadAction(IMethodObserver):
    def __init__(self, thread_registry: IThreadRegistry, thread_template: ThreadTemplate) -> None:
        self._thread_registry: IThreadRegistry = thread_registry
        self._template: ThreadTemplate = thread_template

    def execute(self) -> None:
        thread = self._thread_registry.create_thread_instance(self._template)

    def method_notify(self, event: str, method: Method) -> None:
        if event == MethodStatus.IN_PROGRESS:
            self._template.set_wrapped_method(method)
            self.execute()