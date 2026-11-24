"""How long a device that dropped mid-command may stay away before its command fails.

When a device-bridge WebSocket disconnects with a command in flight, the
controller holds the pending future and arms a disconnect timer. A
reconnect within the grace fails the command with ``DeviceOfflineError``
and nothing is sent again; so does the grace running out. Every device
gets the same grace.
"""

from typing import Protocol


class IDisconnectGrace(Protocol):
    """How long a device may stay disconnected with a command in flight."""

    @property
    def seconds(self) -> float: ...


class DisconnectGrace:
    """The disconnect grace every device gets."""

    DEFAULT_SECONDS: float = 300.0

    def __init__(self, *, seconds: float = DEFAULT_SECONDS) -> None:
        if seconds <= 0:
            raise ValueError(f"seconds must be > 0, got {seconds}")
        self._seconds = seconds

    @property
    def seconds(self) -> float:
        return self._seconds
