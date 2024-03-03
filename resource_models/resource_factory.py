from importlib.resources import Resource
from typing import Any, Dict, List

from drivers.base_resource import BaseEquipmentResource, IResource
from drivers.drivers import VenusProtocol, MockResource, MockNonLabwareableResource, MockRoboticArm
from resource_models.resource_pool import EquipmentResourcePool
from system import System


class ResourceFactory:

    def create(self, resource_name: str, resource_config: Dict[str, Any]) -> IResource:
        if 'type' not in resource_config.keys():
                raise KeyError("No resource type defined in config")
        res_type = resource_config['type']
        if res_type == 'venus-method':
            resource = VenusProtocol(resource_name)
        elif res_type == 'acell':
            resource = MockRoboticArm(resource_name, "ACell")
        elif res_type == 'mock-robot':
            resource = MockRoboticArm(resource_name, "Precision Flex")
        elif res_type == 'ddr':
            resource = MockRoboticArm(resource_name, "DDR")
        elif res_type == 'translator':
            resource = MockRoboticArm(resource_name, "Translator")
        elif res_type == 'cwash':
            resource = MockResource(resource_name, "CWash")
        elif res_type == 'mantis':
            resource = MockResource(resource_name, "Mantis")
        elif res_type == 'analytic-jena':
            resource = MockResource(resource_name, "Analytic Jena")
        elif res_type == 'tapestation-4200':
            resource = MockResource(resource_name,"Tapestation 4200")
        elif res_type == 'biotek':
            resource = MockResource(resource_name, "Biotek")
        elif res_type == 'bravo':
            resource = MockResource(resource_name, "Bravo")
        elif res_type == 'plateloc':
            resource = MockResource(resource_name, "Plateloc")
        elif res_type == 'vspin':
            resource = MockResource(resource_name, "VSpin")
        elif res_type == 'agilent-hotel':
            resource = MockResource(resource_name, "Agilent Hotel")
        elif res_type == 'smc-pro':
            resource = MockResource(resource_name, "SMC Pro")
        elif res_type == 'vstack':
            resource = MockResource(resource_name, "VStack")
        elif res_type == 'shaker':
            resource = MockResource(resource_name, "Shaker")
        elif res_type == 'waste':
            resource = MockResource(resource_name, "Waste")
        elif res_type == 'serial-switch':
            resource = MockNonLabwareableResource(resource_name, "Serial Switch")
        elif res_type == 'switch':
            resource = MockNonLabwareableResource(resource_name, "Switch")
        else:
            raise ValueError(f"Unknown resource type: {res_type}")
        resource.set_init_options(resource_config)
        return resource
    
class ResourcePoolFactory:
    def __init__(self, system: System) -> None:
        self._system = system

    def create(self, pool_name: str, pool_config: Dict[str, Any]) -> EquipmentResourcePool:
        if 'type' not in pool_config.keys():
            raise KeyError("No resource type defined in config")
        
        res_type = pool_config['type']
        if res_type != 'pool':
            raise ValueError(f"Resource pool {pool_name} type set as {res_type} instead of 'pool'")
        pool: EquipmentResourcePool = EquipmentResourcePool(pool_name)
        if "resources" not in pool_config.keys():
            raise KeyError(f"No resources defined in resource pool {pool_name}")
        resources: List[BaseEquipmentResource] = []
        for res_name in pool_config['resources']:
            if res_name not in self._system.resources.keys():
                raise KeyError(f"Resource {res_name} from resource pool {pool_name} not found in system")
            res = self._system.resources[res_name]
            if not isinstance(res, BaseEquipmentResource):
                raise ValueError(f"Resource {res_name} from resource pool {pool_name} is not a valid equipment resource")
            resources.append(res)
        pool.set_resources(resources)
        return pool    