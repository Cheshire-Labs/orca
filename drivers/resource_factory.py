from typing import Any, Dict

from drivers.base_resource import IResource
from drivers.drivers import VenusProtocol, MockResource, MockRoboticArm


class ResourceFactory:
    @staticmethod
    def create(resource_name: str, resource_config: Dict[str, Any]) -> IResource:
        if 'type' not in resource_config.keys():
                raise KeyError("No resource type defined in config")
        res_type = resource_config['type']
        if res_type == 'venus-method':
            resource = VenusProtocol(resource_name)
            
        elif res_type == 'cwash':
            resource = MockResource(resource_name, "CWash")
        elif res_type == 'mantis':
            resource = MockResource(resource_name, "Mantis")
        elif res_type == 'analytic-jena':
            resource = MockResource(resource_name, "Analytic Jena")
        elif res_type == 'tapestation-4200':
            resource = MockResource(resource_name,"Tapestation 4200")
        elif res_type == 'acell':
            resource = MockRoboticArm(resource_name, "ACell")
        elif res_type == 'mock-robot':
            resource = MockRoboticArm(resource_name, "ACell")
        else:
            raise ValueError(f"Unknown resource type: {res_type}")
        resource.set_init_options(resource_config)
        return resource