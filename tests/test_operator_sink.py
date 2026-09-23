"""Tests for OperatorSink -- dedicated file logging for operator instructions.

manual_step() already logs to the 'orca.operator' logger. OperatorSink
attaches a FileHandler so those messages are captured in a dedicated file
(default: orca_operator.log) for operator visibility.
"""

import logging
import os
import tempfile

import pytest

from orca.runtime.sinks import OperatorSink


class TestOperatorSink:

    def test_creates_file_handler(self, tmp_path: object) -> None:
        logfile = os.path.join(str(tmp_path), "orca_operator.log")
        sink = OperatorSink(filepath=logfile)
        try:
            logger = logging.getLogger("orca.operator")
            assert any(
                isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(logfile)
                for h in logger.handlers
            )
        finally:
            sink.close()

    def test_captures_operator_messages(self, tmp_path: object) -> None:
        logfile = os.path.join(str(tmp_path), "orca_operator.log")
        sink = OperatorSink(filepath=logfile)
        try:
            logger = logging.getLogger("orca.operator")
            logger.info("ACTION REQUIRED [manual_step-abc1]: Remove plate and centrifuge at 300g")

            sink.flush()
            with open(logfile, "r") as f:
                content = f.read()
            assert "Remove plate and centrifuge at 300g" in content
            assert "manual_step-abc1" in content
        finally:
            sink.close()

    def test_close_removes_handler(self, tmp_path: object) -> None:
        logfile = os.path.join(str(tmp_path), "orca_operator.log")
        sink = OperatorSink(filepath=logfile)
        logger = logging.getLogger("orca.operator")

        handler_count_before = len(logger.handlers)
        sink.close()
        assert len(logger.handlers) == handler_count_before - 1

    def test_does_not_capture_other_loggers(self, tmp_path: object) -> None:
        logfile = os.path.join(str(tmp_path), "orca_operator.log")
        sink = OperatorSink(filepath=logfile)
        try:
            other_logger = logging.getLogger("orca.events")
            other_logger.info("This should not appear in operator log")

            sink.flush()
            with open(logfile, "r") as f:
                content = f.read()
            assert "This should not appear" not in content
        finally:
            sink.close()

    def test_default_filepath(self) -> None:
        sink = OperatorSink()
        try:
            assert sink.filepath == "orca_operator.log"
        finally:
            sink.close()
            if os.path.exists("orca_operator.log"):
                os.remove("orca_operator.log")

    def test_context_manager(self, tmp_path: object) -> None:
        logfile = os.path.join(str(tmp_path), "orca_operator.log")
        logger = logging.getLogger("orca.operator")

        with OperatorSink(filepath=logfile) as sink:
            logger.info("ACTION REQUIRED [step-1]: Check wells")

        # Handler should be removed after context exit
        sink_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.FileHandler)
            and h.baseFilename == os.path.abspath(logfile)
        ]
        assert len(sink_handlers) == 0

        with open(logfile, "r") as f:
            assert "Check wells" in f.read()
