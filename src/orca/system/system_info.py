from orca.system.interfaces import ISystemInfo


import uuid
from typing import Dict


class SystemInfo(ISystemInfo):
    def __init__(self, name: str, version: str, description: str, model_extra: Dict[str, str]) -> None:
        self._id = str(uuid.uuid4())
        self._name = name
        self._version = version
        self._description = description
        self._model_extra = model_extra

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return self._description

    @property
    def model_extra(self) -> Dict[str, str]:
        return self._model_extra