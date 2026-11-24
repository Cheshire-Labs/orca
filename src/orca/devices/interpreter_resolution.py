from orca.devices.device_interfaces import ITrackedDevice
from orca.resource_models.tracking_interpreter import DefaultInterpreter, IOperationInterpreter


def resolve_operation_interpreter(device: object) -> IOperationInterpreter:
    """Pick the IOperationInterpreter for ``device`` by walking its MRO.

    Returns the first ``ITrackedDevice`` subclass in ``type(device).__mro__``
    (excluding ``ITrackedDevice`` itself) that explicitly defines its own
    ``operation_interpreter`` classmethod. Falls through to ``DefaultInterpreter``
    when no tracked interface is found, which is correct for untracked devices
    (DefaultInterpreter emits a generic record per call) but would silently
    drop typed records for tracked ones; the build-time guard in
    ``orca.sdk.build`` rejects that case loud.
    """
    for cls in type(device).__mro__:
        if not isinstance(cls, type) or cls is ITrackedDevice:
            continue
        if not issubclass(cls, ITrackedDevice):
            continue
        own = cls.__dict__.get("operation_interpreter")
        if own is None:
            continue
        return cls.operation_interpreter()
    return DefaultInterpreter()
