from typing import Any, Dict, List
from drivers.base_resource import IResource


class ResourcePool:
    def __init__(self, name: str, resources: List[IResource], options: Dict[str, Any] = {}):
        self._name = name
        self._resources: List[IResource] = resources
        self._options: Dict[str, Any] = options

    def get_available_resource(self):
        return [res for res in self._resources if not res.is_running()][0]