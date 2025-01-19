import asyncio
import logging
from logging import Handler, LogRecord
from typing import Optional, Union, Literal
import socketio


# Global variables
_logging_initialized = False
_socketio_handler: Optional["SocketIOHandler"] = None
_formatter: Optional[logging.Formatter] = None
LOGGING_NAMESPACE = "/logging"

orca_logger = logging.getLogger("orca")


def get_formatter() -> logging.Formatter:
    """Get the global formatter instance, creating it if necessary."""
    global _formatter  # pylint: disable=W0603
    if _formatter is None:
        _formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
    return _formatter


class SocketIOHandler(Handler):
    """Logging handler emitting messages via Socket.IO."""

    def __init__(self, sio: socketio.AsyncServer):
        super().__init__()
        self.sio = sio
        self.loop = None

    def emit(self, record: LogRecord) -> None:
        msg = self.format(record)
        message = {
            "level": record.levelname,
            "message": msg,
            "timestamp": record.created,
        }

        loop = self.loop or asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(self._send_log_message(message))
        else:
            future = asyncio.run_coroutine_threadsafe(
                self._send_log_message(message), loop
            )
            future.result()

    async def _send_log_message(self, message: dict) -> None:
        """Coroutine to send log message via Socket.IO."""
        try:
            await self.sio.emit("log_message", message, namespace=LOGGING_NAMESPACE)
        except Exception as e:
            print(f"Failed to emit log message: {e}")


def get_socketio_handler() -> Optional["SocketIOHandler"]:
    """Returns global SocketIOHandler instance."""
    global _socketio_handler  # pylint: disable=W0602
    return _socketio_handler


def setup_logging(
    sio: Optional[socketio.AsyncServer] = None,
    destination: Optional[Union[str, logging.Handler]] = None,
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
) -> None:
    """Centralized logging setup for all Orca components."""
    global _logging_initialized, _socketio_handler  # pylint: disable=W0603

    if _logging_initialized:
        return

    orca_logger.setLevel(getattr(logging, level))
    orca_logger.handlers.clear()

    formatter = get_formatter()

    # Add console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    orca_logger.addHandler(console_handler)

    # Add SocketIO handler if provided
    if sio:
        _socketio_handler = SocketIOHandler(sio)
        _socketio_handler.setFormatter(formatter)
        orca_logger.addHandler(_socketio_handler)
        orca_logger.info("SocketIO logging initialized")

    # Add custom destination if provided
    if destination:
        if isinstance(destination, str):
            file_handler = logging.FileHandler(destination)
            file_handler.setFormatter(formatter)
            orca_logger.addHandler(file_handler)
        elif isinstance(destination, logging.Handler):
            destination.setFormatter(formatter)
            orca_logger.addHandler(destination)

    _logging_initialized = True
