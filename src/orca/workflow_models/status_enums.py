from enum import Enum, auto

class ActionStatus(Enum):
    CREATED = auto()
    RESOLVED = auto()
    AWAITING_LOCATION_RESERVATION = auto()
    AWAITING_CO_THREADS = auto()
    PERFORMING_ACTION = auto()
    AWAITING_MOVE_RESERVATION = auto()
    MOVING = auto()
    COMPLETED = auto()
    ERRORED = auto()

class MethodStatus(Enum):
    CREATED = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()

class LabwareThreadStatus(Enum):
    UNCREATED = auto()
    CREATED = auto()
    AWAITING_ACTION_RESERVATION = auto()
    AWAITING_MOVE_RESERVATION = auto()
    AWAITING_MOVE_TARGET_AVAILABILITY = auto()
    MOVING = auto()
    AWAITING_CO_THREADS = auto()
    PERFORMING_ACTION = auto()    
    COMPLETED = auto()
    STOPPING = auto()
    STOPPED = auto()



