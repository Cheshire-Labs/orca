from drivers.base_resource import IResource


from typing import Optional


class Location:
    def __init__(self, name: str) -> None:
        self._name = name
        self._is_available = True
        self._resource: Optional[IResource] = None
        self._can_resolve_deadlock = True

    def set_resource(self, resource: IResource, can_resolve_deadlock: bool = False) -> None:
        self._can_resolve_deadlock = can_resolve_deadlock
        self._resource = resource