from enum import Enum, auto

class ActionStatus(Enum):
    CREATED = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    ERRORED = auto()

class MethodStatus(Enum):
    CREATED = auto()
    READY = auto()
    IN_PROGRESS = auto()
    AWAITING_RESOURCE = auto()
    AWAITING_LABWARE = auto()
    COMPLETED = auto()

class LabwareThreadStatus(Enum):
    CREATED = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()

