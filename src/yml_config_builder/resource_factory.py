from typing import List
from config_interfaces import IResourceConfig, IResourcePoolConfig
from drivers.driver_socket_client import RemoteLabwarePlaceableDriverClient
from drivers.drivers import SimulationBaseDriver, SimulationDriver, SimulationRoboticArm
from resource_models.base_resource import Equipment, IEquipment, LabwareLoadableEquipment

from resource_models.resource_pool import EquipmentResourcePool
from drivers.transporter_resource import TransporterEquipment
from system.resource_registry import IResourceRegistry

class ResourceFactory:

    def create(self, resource_name: str, resource_config: IResourceConfig) -> IEquipment:
        resource: IEquipment
        res_type = resource_config.type
        if res_type == 'ml-star':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Hamilton MLSTAR", "Hamilton MLSTAR"))
        elif res_type == 'venus':
            if resource_config.sim:
                resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Venus", "Venus"))
            else:
                resource = LabwareLoadableEquipment(resource_name, RemoteLabwarePlaceableDriverClient("venus"))
        elif res_type == 'acell':
            resource = TransporterEquipment(resource_name, SimulationRoboticArm(resource_name, "ACell"))
        elif res_type == 'mock-robot':
            resource = TransporterEquipment(resource_name, SimulationRoboticArm(resource_name, "Precision Flex"))
        elif res_type == 'ddr':
            resource = TransporterEquipment(resource_name, SimulationRoboticArm(resource_name, "DDR"))
        elif res_type == 'translator':
            resource = TransporterEquipment(resource_name, SimulationRoboticArm(resource_name, "Translator"))
        elif res_type == 'cwash':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("CWash", "CWash"))
        elif res_type == 'mantis':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Mantis", "Mantis"))
        elif res_type == 'analytic-jena':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Analytic Jena", "Analytic Jena"))
        elif res_type == 'tapestation-4200':
            resource = LabwareLoadableEquipment(resource_name,SimulationDriver("Tapestation 4200", "Tapestation 4200"))
        elif res_type == 'biotek':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Biotek", "Biotek"))
        elif res_type == 'bravo':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Bravo", "Bravo"))
        elif res_type == 'plateloc':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Plateloc", "Plateloc"))
        elif res_type == 'vspin':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("VSpin", "VSpin"))
        elif res_type == 'agilent-hotel':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Agilent Hotel", "Agilent Hotel"))
        elif res_type == 'smc-pro':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("SMC Pro", "SMC Pro"))
        elif res_type == 'vstack':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("VStack", "VStack"))
        elif res_type == 'shaker':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Shaker", "Shaker"))
        elif res_type == 'waste':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Waste", "Waste"))
        elif res_type == 'delidder':
            resource = LabwareLoadableEquipment(resource_name, SimulationDriver("Delidder", "Delidder"))
        elif res_type == 'serial-switch':
            resource = Equipment(resource_name, SimulationBaseDriver("Serial Switch", "Serial Switch"))
        elif res_type == 'switch':
            resource = Equipment(resource_name, SimulationBaseDriver("Switch", "Switch"))
        else:
            raise ValueError(f"Unknown resource type: {res_type}")
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