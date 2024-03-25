from enum import Enum, auto


class MethodStatus(Enum):
    CREATED = auto()
    AWAITING_RESOURCES = auto()
    READY = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()

class LabwareThreadStatus(Enum):
    CREATED = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()


class ActionStatus(Enum):
    CREATED = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    ERRORED = auto()