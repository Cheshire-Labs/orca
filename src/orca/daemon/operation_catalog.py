"""The Operations the local daemon serves."""

from collections.abc import Sequence
from typing import Protocol

from orca.api import GenericOperationCatalog, OperationEntry
from orca.daemon.api_context import DaemonApiContext
from orca.operations.binding import BoundOperation
from orca.operations.system_loading import DAEMON_LOADING_OPERATIONS
from orca.runtime.runtime_holder import IRuntimeHolder


class _TakesTheHolder(BoundOperation, Protocol):
    def __init__(self, *, holder: IRuntimeHolder) -> None: ...


def _with_holder(op_class: type[_TakesTheHolder]) -> OperationEntry[DaemonApiContext]:
    return OperationEntry(op_class, lambda context: op_class(holder=context.runtime_holder()))


class DaemonOperationCatalog:
    def entries(self) -> Sequence[OperationEntry[DaemonApiContext]]:
        return (
            *GenericOperationCatalog().entries(),
            *(_with_holder(op) for op in DAEMON_LOADING_OPERATIONS),
        )
