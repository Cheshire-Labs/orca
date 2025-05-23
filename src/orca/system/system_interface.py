from orca.system.thread_manager_interface import IThreadManager
from orca.system.interfaces import IMethodTemplateRegistry, ISystemInfo, IThreadTemplateRegistry, IWorkflowRegistry, IWorkflowTemplateRegistry
from orca.system.labware_registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry
from orca.system.resource_registry import IResourceRegistry
from orca.system.system_map import ILocationRegistry, SystemMap


from abc import ABC, abstractmethod


class ISystem(ISystemInfo,
              IResourceRegistry,
              ILabwareRegistry,
              ILabwareTemplateRegistry,
              IWorkflowTemplateRegistry,
              IMethodTemplateRegistry,
              IThreadTemplateRegistry,
              ILocationRegistry,
              IWorkflowRegistry,
              IThreadManager,
              ABC):

    @property
    @abstractmethod
    def system_map(self) -> SystemMap:
        raise NotImplementedError