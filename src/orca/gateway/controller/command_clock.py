"""Per-command timeout source for the device gateway.

DeviceCommandClock replaces the per-device-kind ``DEFAULT_TIMEOUTS`` table
with a per-command lookup against the driver's advertised
``MethodInfo.duration`` (populated at handshake from cheshire-drivers'
``command_timings`` ClassVar). A shaker's ``stop`` and its ``shake`` need
wildly different timeouts; the driver class is the only layer that knows.

When a device hasn't advertised a duration for a command -- for example
during a transition window where the device bridge predates the
cheshire-drivers update that introduced ``MethodInfo.duration`` --
the clock falls back to ``fallback_seconds`` (default 10 minutes, well
above any reasonable single-command duration so transient pre-upgrade
clients keep working instead of erroring out).
"""

from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.runtime.recoverable_timeout import DEFAULT_COMMAND_TIMEOUT_SECONDS


class DeviceCommandClock:
    """Resolve the per-command timeout for a device dispatch."""

    DEFAULT_FALLBACK_SECONDS: float = DEFAULT_COMMAND_TIMEOUT_SECONDS

    def __init__(self, *, fallback_seconds: float = DEFAULT_FALLBACK_SECONDS) -> None:
        if fallback_seconds <= 0:
            raise ValueError(
                f"fallback_seconds must be > 0, got {fallback_seconds}"
            )
        self._fallback_seconds = fallback_seconds

    @property
    def fallback_seconds(self) -> float:
        return self._fallback_seconds

    def timeout_for(self, device: DeviceSnapshot, command: str) -> float:
        """Return the timeout to use for ``command`` against ``device``.

        Consults ``device.methods[command].duration.max_seconds`` first;
        falls back to ``fallback_seconds`` when the device hasn't
        advertised a duration for the command.
        """
        info = device.methods.get(command)
        if info is None or info.duration is None:
            return self._fallback_seconds
        return float(info.duration.max_seconds)
