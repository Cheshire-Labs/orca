from resource_models.base_resource import BaseLabwareableResource

class PlatePad(BaseLabwareableResource):

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._can_resolve_deadlock = True
        self._is_initialized = False

    def initialize(self) -> bool:
        self._is_initialized = True
        return self._is_initialized