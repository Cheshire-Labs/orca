from enum import Enum, auto

class ActionStatus(Enum):
    CREATED = auto()
    RESOLVED = auto()
    AWAITING_LOCATION_RESERVATION = auto()
    AWAITING_CO_THREADS = auto()
    EXECUTING_ACTION = auto()
    AWAITING_MOVE_RESERVATION = auto()
    PREPARING_TO_MOVE = auto()
    PICKING = auto()
    PLACING = auto()
    COMPLETED = auto()
    ERRORED = auto()

class MethodStatus(Enum):
    CREATED = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()

class LabwareThreadStatus(Enum):
    CREATED = auto()
    RESOLVING_ACTION_LOCATION = auto()
    ACTION_LOCATION_RESOLVED = auto()
    AWAITING_MOVE_RESERVATION = auto()
    AWAITING_MOVE_TARGET_AVAILABILITY = auto()
    MOVING = auto()
    AWAITING_CO_THREADS = auto()
    EXECUTING_ACTION = auto()    
    COMPLETED = auto()
    STOPPING = auto()
    STOPPED = auto()

class WorkflowStatus(Enum):
    CREATED = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    ERRORED = auto()



