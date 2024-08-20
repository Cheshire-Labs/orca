from typing import Callable, Dict, List
from abc import ABC, abstractmethod
from orca.config_interfaces import IResourceConfig, IResourcePoolConfig
from orca.drivers.driver_socket_client import RemoteLabwarePlaceableDriverClient
from orca.drivers.drivers import SimulationBaseDriver, SimulationDriver, SimulationRoboticArmDriver
from orca.helper import FilepathReconciler
from orca.resource_models.base_resource import Equipment, IEquipment, LabwareLoadableEquipment

from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.drivers.transporter_resource import TransporterEquipment
from orca.system.resource_registry import IResourceRegistry

class IResourceFactory(ABC):
    @abstractmethod
    def create(self, resource_name: str, resource_config: IResourceConfig) -> IEquipment:
        raise NotImplementedError
    
class ResourceFactory(IResourceFactory):
    def __init__(self, filepath_reconciler: FilepathReconciler) -> None:
        self._resource_map: Dict[str, Callable[[str, IResourceConfig], IEquipment]] = {
            'ml-star': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Hamilton MLSTAR", "Hamilton MLSTAR")),
            'venus': self.create_venus,
            'acell': lambda name, config: TransporterEquipment(name, SimulationRoboticArmDriver(name, filepath_reconciler, "ACell")),
            'mock-robot': lambda name, config: TransporterEquipment(name, SimulationRoboticArmDriver(name, filepath_reconciler, "Precision Flex" )),
            'ddr': lambda name, config: TransporterEquipment(name, SimulationRoboticArmDriver(name, filepath_reconciler, "DDR")),
            'translator': lambda name, config: TransporterEquipment(name, SimulationRoboticArmDriver(name, filepath_reconciler, "Translator")),
            'cwash': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("CWash", "CWash")),
            'mantis': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Mantis", "Mantis")),
            'analytic-jena': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Analytic Jena", "Analytic Jena")),
            'tapestation-4200': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Tapestation 4200", "Tapestation 4200")),
            'biotek': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Biotek", "Biotek")),
            'bravo': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Bravo", "Bravo")),
            'plateloc': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Plateloc", "Plateloc")),
            'vspin': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("VSpin", "VSpin")),
            'agilent-hotel': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Agilent Hotel", "Agilent Hotel")),
            'smc-pro': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("SMC Pro", "SMC Pro")),
            'vstack': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("VStack", "VStack")),
            'shaker': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Shaker", "Shaker")),
            'waste': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Waste", "Waste")),
            'delidder': lambda name, config: LabwareLoadableEquipment(name, SimulationDriver("Delidder", "Delidder")),
            'serial-switch': lambda name, config: Equipment(name, SimulationBaseDriver("Serial Switch", "Serial Switch")),
            'switch': lambda name, config: Equipment(name, SimulationBaseDriver("Switch", "Switch"))
        }

    def create_venus(self, resource_name: str, resource_config: IResourceConfig) -> IEquipment:
        if resource_config.sim:
            return LabwareLoadableEquipment(resource_name, SimulationDriver("Venus", "Venus"))
        else:
            return LabwareLoadableEquipment(resource_name, RemoteLabwarePlaceableDriverClient("venus"))

    def create(self, resource_name: str, resource_config: IResourceConfig) -> IEquipment:
        res_type = resource_config.type
        if res_type not in self._resource_map:
            raise ValueError(f"Unknown resource type: {res_type}")
        resource = self._resource_map[res_type](resource_name, resource_config)
        resource.set_init_options(resource_config.options)
        return resource
    
class ResourcePoolFactory:
    def __init__(self, resource_reg: IResourceRegistry) -> None:
        self._resource_reg = resource_reg

    def create(self, pool_name: str, pool_config: IResourcePoolConfig) -> EquipmentResourcePool:
        res_type = pool_config.type
        if res_type != 'pool':
            raise ValueError(f"Resource pool {pool_name} type set as {res_type} instead of 'pool'")
        resources: List[Equipment] = []
        for res_name in pool_config.resources:
            try:
                res = self._resource_reg.get_resource(res_name)
            except KeyError:
                raise KeyError(f"Resource {res_name} from resource pool {pool_name} not found in system")
            if not isinstance(res, Equipment):
                raise ValueError(f"Resource {res_name} from resource pool {pool_name} is not a valid equipment resource")
            resources.append(res)
            
        return EquipmentResourcePool(pool_name, resources)    