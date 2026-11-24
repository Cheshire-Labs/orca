"""The typed refusal for a connected device that advertises fewer interfaces than Orca expects of it."""

from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.errors import TopologyCollisionError


def interface_mismatch(exc: TopologyCollisionError) -> OperationError:
    """The device's orca-client cannot run what the setup says its driver implements: 409, naming the device."""
    return OperationError.of(ErrorCode.DEVICE_INTERFACE_MISMATCH, str(exc), device_id=exc.device_name)
