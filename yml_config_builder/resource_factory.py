from typing import List
from resource_models.drivers import PlaceHolderNonLabwareResource, PlaceHolderResource, PlaceHolderRoboticArm
from resource_models.base_resource import BaseResource, Equipment

from resource_models.resource_pool import EquipmentResourcePool
from workflow_models.workflow_templates import SystemTemplate
from yml_config_builder.configs import ResourceConfig, ResourcePoolConfig




class ResourceFactory:

    def create(self, resource_name: str, resource_config: ResourceConfig) -> BaseResource:
        resource: BaseResource
        res_type = resource_config.type
        if res_type == 'ml-star':
            resource = PlaceHolderResource(resource_name, mocking_type="Hamilton MLSTAR")
        elif res_type == 'acell':
            resource = PlaceHolderRoboticArm(resource_name, "ACell")
        elif res_type == 'mock-robot':
            resource = PlaceHolderRoboticArm(resource_name, "Precision Flex")
        elif res_type == 'ddr':
            resource = PlaceHolderRoboticArm(resource_name, "DDR")
        elif res_type == 'translator':
            resource = PlaceHolderRoboticArm(resource_name, "Translator")
        elif res_type == 'cwash':
            resource = PlaceHolderResource(resource_name, "CWash")
        elif res_type == 'mantis':
            resource = PlaceHolderResource(resource_name, "Mantis")
        elif res_type == 'analytic-jena':
            resource = PlaceHolderResource(resource_name, "Analytic Jena")
        elif res_type == 'tapestation-4200':
            resource = PlaceHolderResource(resource_name,"Tapestation 4200")
        elif res_type == 'biotek':
            resource = PlaceHolderResource(resource_name, "Biotek")
        elif res_type == 'bravo':
            resource = PlaceHolderResource(resource_name, "Bravo")
        elif res_type == 'plateloc':
            resource = PlaceHolderResource(resource_name, "Plateloc")
        elif res_type == 'vspin':
            resource = PlaceHolderResource(resource_name, "VSpin")
        elif res_type == 'agilent-hotel':
            resource = PlaceHolderResource(resource_name, "Agilent Hotel")
        elif res_type == 'smc-pro':
            resource = PlaceHolderResource(resource_name, "SMC Pro")
        elif res_type == 'vstack':
            resource = PlaceHolderResource(resource_name, "VStack")
        elif res_type == 'shaker':
            resource = PlaceHolderResource(resource_name, "Shaker")
        elif res_type == 'waste':
            resource = PlaceHolderResource(resource_name, "Waste")
        elif res_type == 'delidder':
            resource = PlaceHolderResource(resource_name, "Delidder")
        elif res_type == 'serial-switch':
            resource = PlaceHolderNonLabwareResource(resource_name, "Serial Switch")
        elif res_type == 'switch':
            resource = PlaceHolderNonLabwareResource(resource_name, "Switch")
        else:
            raise ValueError(f"Unknown resource type: {res_type}")
        resource.set_init_options(resource_config.model_extra)
        return resource
    
class ResourcePoolFactory:
    def __init__(self, system: SystemTemplate) -> None:
        self._system = system

    def create(self, pool_name: str, pool_config: ResourcePoolConfig) -> EquipmentResourcePool:
        res_type = pool_config.type
        if res_type != 'pool':
            raise ValueError(f"Resource pool {pool_name} type set as {res_type} instead of 'pool'")
        resources: List[Equipment] = []
        for res_name in pool_config.resources:
            if res_name not in self._system.resources.keys():
                raise KeyError(f"Resource {res_name} from resource pool {pool_name} not found in system")
            res = self._system.resources[res_name]
            if not isinstance(res, Equipment):
                raise ValueError(f"Resource {res_name} from resource pool {pool_name} is not a valid equipment resource")
            resources.append(res)
            
        return EquipmentResourcePool(pool_name, resources)    