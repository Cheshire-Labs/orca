from typing import Any, Callable, Dict, List, cast
from abc import ABC, abstractmethod
from orca.config_interfaces import IResourceConfig, IResourcePoolConfig
from orca.driver_management.driver_installer import IDriverManager
from orca.driver_management.driver_socket_client import RemoteLabwarePlaceableDriverClient
from orca.driver_management.drivers.simulation_base.simulation_base import SimulationBaseDriver
from orca.driver_management.drivers.simulation_robotic_arm.simulation_robotic_arm import SimulationRoboticArmDriver
from orca.driver_management.drivers.simulation_labware_placeable.simulation_labware_placeable import SimulationLabwarePlaceableDriver
from orca.helper import FilepathReconciler
from orca.resource_models.base_resource import Equipment, IEquipment, LabwareLoadableEquipment

from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.resource_models.transporter_resource import TransporterEquipment
from orca.system.resource_registry import IResourceRegistry
from orca_driver_interface.driver_interfaces import IDriver, ILabwarePlaceableDriver
from orca_driver_interface.transporter_interfaces import ITransporterDriver

class IResourceFactory(ABC):
    @abstractmethod
    def create(self, resource_name: str, resource_config: IResourceConfig) -> IEquipment:
        raise NotImplementedError
    
class ResourceFactory(IResourceFactory):
    def __init__(self, driver_manager: IDriverManager, filepath_reconciler: FilepathReconciler) -> None:
        self._driver_manager = driver_manager


        self._resource_map: Dict[str, Callable[[str, IResourceConfig], IEquipment]] = {
            'mock-labware-loadable': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Mock Labware Loadable", "Mock Labware Loadable")),
            'mock-robot': lambda name, config: TransporterEquipment(name, SimulationRoboticArmDriver(name, filepath_reconciler, "Mock Robot" )),
            'ml-star': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Hamilton MLSTAR", "Hamilton MLSTAR")),
            'venus': lambda name, config: LabwareLoadableEquipment(name, cast(ILabwarePlaceableDriver, self._driver_manager.get_driver("venus"))),
            'test': lambda name, config: LabwareLoadableEquipment(name, cast(ILabwarePlaceableDriver, self._driver_manager.get_driver('test-driver'))),
            'acell': lambda name, config: TransporterEquipment(name, SimulationRoboticArmDriver(name, filepath_reconciler, "ACell")),
            'ddr': lambda name, config: TransporterEquipment(name, SimulationRoboticArmDriver(name, filepath_reconciler, "DDR")),
            'translator': lambda name, config: TransporterEquipment(name, SimulationRoboticArmDriver(name, filepath_reconciler, "Translator")),
            'cwash': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("CWash", "CWash")),
            'mantis': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Mantis", "Mantis")),
            'analytic-jena': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Analytic Jena", "Analytic Jena")),
            'tapestation-4200': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Tapestation 4200", "Tapestation 4200")),
            'biotek': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Biotek", "Biotek")),
            'bravo': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Bravo", "Bravo")),
            'plateloc': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Plateloc", "Plateloc")),
            'vspin': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("VSpin", "VSpin")),
            'agilent-hotel': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Agilent Hotel", "Agilent Hotel")),
            'smc-pro': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("SMC Pro", "SMC Pro")),
            'vstack': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("VStack", "VStack")),
            'shaker': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Shaker", "Shaker")),
            'waste': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Waste", "Waste")),
            'delidder': lambda name, config: LabwareLoadableEquipment(name, SimulationLabwarePlaceableDriver("Delidder", "Delidder")),
            'serial-switch': lambda name, config: Equipment(name, SimulationBaseDriver("Serial Switch", "Serial Switch")),
            'switch': lambda name, config: Equipment(name, SimulationBaseDriver("Switch", "Switch"))
        }

    def create_venus(self, resource_name: str, resource_config: IResourceConfig) -> IEquipment:
        if resource_config.sim:
            return LabwareLoadableEquipment(resource_name, SimulationLabwarePlaceableDriver("Venus", "Venus"))
        else:
            return LabwareLoadableEquipment(resource_name, RemoteLabwarePlaceableDriverClient("venus"))

    def create(self, resource_name: str, resource_config: IResourceConfig) -> IEquipment:

        # TODO:  Something needs to be done here to decide what the sim looks like for each of these different types of equipment

        # TODO: inits for drivers should be paramterless

        res_type = resource_config.type
        if resource_config.sim:
            driver = self._get_similation_driver(res_type)
        else:
            driver = self._get_real_driver(res_type)
        
        if isinstance(driver, ILabwarePlaceableDriver):
            equipment: IEquipment = LabwareLoadableEquipment(resource_name, driver)
        elif isinstance(driver, ITransporterDriver):
            equipment = TransporterEquipment(resource_name, driver)
        else:
            equipment = Equipment(resource_name, driver)
        
        equipment.set_init_options(resource_config.options)
        return equipment
    
    def _get_similation_driver(self, res_type: str) -> IDriver:
        simulation_drivers: Dict[str, Any] = {
            'base': SimulationLabwarePlaceableDriver,
            'transporter': SimulationRoboticArmDriver,
            'non-labware': SimulationBaseDriver
            # Add other simulation driver types as needed
        }
        simulation_driver = simulation_drivers.get(res_type, SimulationLabwarePlaceableDriver)
        return simulation_driver()

    def _get_real_driver(self, driver_name: str) -> IDriver:
         return self._driver_manager.get_driver(driver_name)
    
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