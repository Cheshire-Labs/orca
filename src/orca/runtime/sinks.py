"""Built-in event sinks for SystemRuntime.

LogSink: logs RuntimeEvents as structured JSON via Python logging.
CollectorSink: collects events in a list for testing.
"""

import json
import logging
from types import TracebackType

from orca.events.runtime_event import RuntimeEvent

logger = logging.getLogger("orca.events")


class LogSink:
    """Logs each RuntimeEvent as a structured JSON line."""

    def on_event(self, event: RuntimeEvent) -> None:
        logger.info(json.dumps(event.to_dict(), default=str))


class CollectorSink:
    """Collects RuntimeEvents in a list. For testing and inspection."""

    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def on_event(self, event: RuntimeEvent) -> None:
        self.events.append(event)

    def clear(self) -> None:
        self.events.clear()


alert_logger = logging.getLogger("orca.alerts")


class AlertSink:
    """Logs PAUSED thread events to orca.alerts at ERROR level."""

    def on_event(self, event: RuntimeEvent) -> None:
        if event.status == "PAUSED":
            alert_logger.error(
                "ALERT: %s '%s' is PAUSED (execution %s)",
                event.entity_type,
                event.entity_id,
                event.execution_id,
            )


operator_logger = logging.getLogger("orca.operator")


class OperatorSink:
    """Dedicated file sink for operator instructions.

    Attaches a FileHandler to the 'orca.operator' logger so that
    manual_step() instructions are written to a standalone log file.
    Use as a context manager or call close() explicitly.
    """

    def __init__(self, filepath: str = "orca_operator.log") -> None:
        self._filepath = filepath
        self._handler = logging.FileHandler(filepath)
        self._handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        operator_logger.addHandler(self._handler)
        operator_logger.setLevel(logging.INFO)

    @property
    def filepath(self) -> str:
        return self._filepath

    def flush(self) -> None:
        self._handler.flush()

    def close(self) -> None:
        self._handler.flush()
        operator_logger.removeHandler(self._handler)
        self._handler.close()

    def __enter__(self) -> "OperatorSink":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
