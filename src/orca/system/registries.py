from types import MappingProxyType
from typing import List, Dict

from orca.system.interfaces import IMethodTemplateRegistry, IWorkflowTemplateRegistry
from orca.system.labware_registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.system.interfaces import IThreadTemplateRegistry
from orca.workflow_models.method_template import MethodTemplate, MethodTemplateNameCollisionError
from orca.workflow_models.thread_template import ThreadTemplate, ThreadTemplateNameCollisionError
from orca.workflow_models.workflow_templates import WorkflowTemplate


class LabwareRegistry(ILabwareRegistry, ILabwareTemplateRegistry):
    def __init__(self) -> None:
        self._labwares: Dict[str, LabwareInstance] = {}
        self._labware_templates: Dict[str, LabwareTemplate] = {}

    @property
    def labwares(self) -> List[LabwareInstance]:
        return list(self._labwares.values())

    def get_labware(self, name: str) -> LabwareInstance:
        return self._labwares[name]
    
    def add_labware(self, labware: LabwareInstance) -> None:
        self._labwares[labware.name] = labware

    def remove_labware(self, labware_id: str) -> LabwareInstance | None:
        """Remove a labware by id from the registry (operator clear surfaces).

        The registry is keyed by name (template_name-{uuid_prefix}) but
        the operator surfaces address labware by id. Linear scan is
        acceptable -- the registry is small and operator clears are not
        on a hot path.
        """
        match_name = None
        for name, instance in self._labwares.items():
            if instance.id == labware_id:
                match_name = name
                break
        if match_name is None:
            return None
        return self._labwares.pop(match_name, None)

    def get_labware_template(self, name: str) -> LabwareTemplate:
        return self._labware_templates[name]

    def add_labware_template(self, labware: LabwareTemplate) -> None:
        self._labware_templates[labware.name] = labware

    @property
    def labware_templates(self) -> List[LabwareTemplate]:
        return list(self._labware_templates.values())

    def clear(self) -> None:
        self._labwares.clear()
        self._labware_templates.clear()


class TemplateRegistry(IThreadTemplateRegistry, IWorkflowTemplateRegistry, IMethodTemplateRegistry):
    def __init__(self) -> None:
        self._labware_thread_templates: Dict[tuple[str, str], ThreadTemplate] = {}
        self._method_templates: Dict[tuple[str, str], MethodTemplate] = {}
        self._workflow_templates: Dict[str, WorkflowTemplate] = {}

    def get_labware_thread_template(self, workflow_name: str, name: str) -> ThreadTemplate:
        return self._labware_thread_templates[(workflow_name, name)]

    def get_labware_thread_templates(self) -> MappingProxyType[tuple[str, str], ThreadTemplate]:
        return MappingProxyType(self._labware_thread_templates)

    def get_workflow_templates(self) -> MappingProxyType[str, WorkflowTemplate]:
        return MappingProxyType(self._workflow_templates)

    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        if name not in self._workflow_templates:
            raise KeyError(f"workflow not found: {name!r}")
        return self._workflow_templates[name]
    
    def get_method_templates(self) -> MappingProxyType[tuple[str, str], MethodTemplate]:
        return MappingProxyType(self._method_templates)

    def get_method_template(self, workflow_name: str, name: str) -> MethodTemplate:
        return self._method_templates[(workflow_name, name)]

    def add_labware_thread_template(self, workflow_name: str, labware_thread: ThreadTemplate) -> None:
        key = (workflow_name, labware_thread.name)
        existing = self._labware_thread_templates.get(key)
        if existing is not None and existing is not labware_thread:
            raise ThreadTemplateNameCollisionError(
                labware_thread.name,
                conflicting_workflow=workflow_name,
                existing_workflow=workflow_name,
                message=(
                    f"Thread template name collision: '{labware_thread.name}' "
                    f"is already registered by workflow '{workflow_name}'. Each "
                    "thread name must be unique within the workflow."
                ),
            )
        self._labware_thread_templates[key] = labware_thread

    def add_method_template(self, workflow_name: str, method: MethodTemplate) -> None:
        key = (workflow_name, method.name)
        existing = self._method_templates.get(key)
        if existing is not None and existing is not method:
            raise MethodTemplateNameCollisionError(
                method.name,
                conflicting_workflow=workflow_name,
                existing_workflow=workflow_name,
                message=(
                    f"Method template name collision: '{method.name}' is "
                    f"already registered by workflow '{workflow_name}'. Each "
                    "method name must be unique within the workflow."
                ),
            )
        self._method_templates[key] = method

    def add_workflow_template(self, workflow: WorkflowTemplate) -> None:
        name = workflow.name
        if name in self._workflow_templates.keys():
            raise KeyError(f"Workflow {name} is already defined in the system.  Each workflow must have a unique name")
        self._workflow_templates[name] = workflow

    def remove_workflow_template(self, name: str) -> WorkflowTemplate | None:
        """Remove the named workflow template. No-op + None when missing.

        Caller is responsible for the method/thread cascade since the
        template registry can't tell which methods/threads belonged
        exclusively to this workflow without inspecting bundled state.
        """
        return self._workflow_templates.pop(name, None)

    def remove_method_template(self, workflow_name: str, name: str) -> MethodTemplate | None:
        """Remove the named method template within a workflow. No-op + None when missing."""
        return self._method_templates.pop((workflow_name, name), None)

    def remove_labware_thread_template(self, workflow_name: str, name: str) -> ThreadTemplate | None:
        """Remove the named thread template within a workflow. No-op + None when missing."""
        return self._labware_thread_templates.pop((workflow_name, name), None)

    def clear(self) -> None:
        self._labware_thread_templates.clear()
        self._method_templates.clear()
        self._workflow_templates.clear()