
from logging import Handler, LogRecord
import socketio # type: ignore
import asyncio
from typing import Dict

class SocketIOHandler(Handler):
    """A logging handler that emits records via Socket.IO."""

    def __init__(self, sio: socketio.AsyncServer):
        super().__init__()
        self.sio = sio

    def emit(self, record: LogRecord) -> None:
        """Emit a log record via Socket.IO."""

        try:
            message = {"data": self.format(record)}
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_log_message(message))
        except Exception as e:
            print(f"Error sending log message: {e}")

    async def _send_log_message(self, message: Dict[str,str]) -> None:
        """Coroutine to send log message via Socket.IO."""
        try:
            await self.sio.emit("logMessage", message, namespace="/logging") # type: ignore
        except Exception as e:
            print(f"Failed to emit log message: {e}")