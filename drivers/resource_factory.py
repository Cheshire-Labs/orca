from typing import Any, Dict

from drivers.base_resource import IResource
from drivers.drivers import VenusProtocol, MockResource, MockNonLabwareableResource, MockRoboticArm
from resource_pool import ResourcePool


class ResourceFactory:
    @staticmethod
    def create(resource_name: str, resource_config: Dict[str, Any]) -> IResource:
        if 'type' not in resource_config.keys():
                raise KeyError("No resource type defined in config")
        res_type = resource_config['type']
        if res_type == 'venus-method':
            resource = VenusProtocol(resource_name)
        if res_type == 'pool':
            resource = ResourcePool(resource_name)
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