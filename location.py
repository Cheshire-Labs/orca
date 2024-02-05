from drivers.base_resource import IResource


from typing import Optional


class Location:
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def in_use(self) -> bool:
        return self._in_use
    
    @in_use.setter
    def in_use(self, in_use: bool) -> None:
        self._in_use = in_use

    @property
    def reserved(self) -> bool:
        return self._reserved
    
    @reserved.setter
    def reserved(self, reserved: bool) -> None:
        self._reserved = reserved
    
    def __init__(self, name: str) -> None:
        self._name = name
        self._in_use = False
        self._reserved = False
        self._resource: Optional[IResource] = None
        self._can_resolve_deadlock = True

    def set_resource(self, resource: IResource, can_resolve_deadlock: bool = False) -> None:
        self._can_resolve_deadlock = can_resolve_deadlock
        self._resource = resource