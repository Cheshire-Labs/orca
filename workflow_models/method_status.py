from enum import Enum, auto


class MethodStatus(Enum):
    CREATED = auto()
    QUEUED = auto()
    AWAITING_RESOURCES = auto()
    READY = auto()
    RUNNING = auto()
    PAUSED = auto()
    ERRORED = auto()
    COMPLETED = auto()
    CANCELED = auto()