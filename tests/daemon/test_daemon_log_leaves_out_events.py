"""daemon.log keeps diagnostics and leaves out runtime events, which are stored in state.db."""

import logging
from pathlib import Path

import pytest

from orca.daemon import __main__ as daemon_main
from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.sinks import LogSink


def test_an_event_is_not_written_to_the_daemon_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = tmp_path / "daemon.log"
    monkeypatch.setattr(daemon_main, "LOG_FILE_PATH", log_file)
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    daemon_main._setup_logging()
    try:
        LogSink().on_event(RuntimeEvent.from_event_bus(
            "THREAD.t1.RUNNING", "exec-1", WorkflowExecutionContext(execution_id="exec-1", workflow_name="w"),
        ))
        logging.getLogger("orca.daemon").warning("a diagnostic line")
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            handler.close()
            root.removeHandler(handler)
        root.setLevel(level)

    text = log_file.read_text(encoding="utf-8")
    assert "a diagnostic line" in text
    assert "THREAD.t1.RUNNING" not in text
