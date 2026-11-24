class DeviceInitializationError(RuntimeError):
    """A device failed to come up during the lazy first-execution bring-up.

    Carries the device name because the driver error underneath rarely holds
    one, and an operator reading a failed execution has nothing else to go on.
    Stays a RuntimeError so callers that already catch bring-up failures that
    way keep working; the driver's own error is on ``cause``.
    """

    def __init__(self, device_name: str, cause: BaseException) -> None:
        super().__init__(f"device '{device_name}' failed to initialize: {cause}")
        self.device_name = device_name
        self.cause = cause


class DevicesNotReadyError(RuntimeError):
    """A run was refused at its start because devices it uses would not come up.

    ``devices`` maps each device to why. A pool is here only when none of its devices came up.
    """

    def __init__(self, failures: list[DeviceInitializationError]) -> None:
        self.failures = failures
        self.devices = {f.device_name: str(f.cause) for f in failures}
        listed = "; ".join(f"{name}: {why}" for name, why in self.devices.items())
        super().__init__(f"The run was not started because these devices are not ready. {listed}")
