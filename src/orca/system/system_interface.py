from abc import ABC, abstractmethod
import typing

from orca.system.thread_manager_interface import IThreadManager
from orca.system.interfaces import IMethodRegistry, IMethodTemplateRegistry, ISystemInfo, IThreadTemplateRegistry, IWorkflowRegistry, IWorkflowTemplateRegistry
from orca.system.labware_registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry
from orca.system.resource_registry import IResourceRegistry
from orca.system.system_map import ILocationRegistry, SystemMap
from orca.system.thread_registry_interface import IThreadRegistry

from orca.workflow_models.labware_threads.executing_labware_thread import IExecutingThreadRegistry
from orca.workflow_models.workflows.executing_workflow import IExecutingWorkflowRegistry

from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflows.workflow_registry import IExecutingMethodRegistry


class ISystem(ISystemInfo,
              IResourceRegistry,
              ILabwareRegistry,
              ILabwareTemplateRegistry,
              IWorkflowTemplateRegistry,
              IMethodTemplateRegistry,
              IThreadTemplateRegistry,
              ILocationRegistry,
              IMethodRegistry,
              IThreadRegistry,
              IExecutingThreadRegistry,
              IWorkflowRegistry,
              IExecutingWorkflowRegistry,
              IThreadManager,
              IExecutingMethodRegistry,
              ABC):

    @property
    @abstractmethod
    def system_map(self) -> SystemMap:
        raise NotImplementedError
    
    @abstractmethod
    def create_and_register_thread_instance(self, template: "ThreadTemplate") -> "LabwareThreadInstance":
        raise NotImplementedError