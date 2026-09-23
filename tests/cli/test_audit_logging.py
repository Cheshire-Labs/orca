"""orca.audit logger persistence test.

The CLI's logging setup wires a rotating file handler on the orca.audit
logger so confirmed @dangerous operations survive daemon restarts. The
in-memory AuditTrail ring buffer (in danger.py) caps at 1000 entries
and dies on process exit; the file handler is the durable trail.
"""

import logging
from pathlib import Path

import pytest

from orca.cli.logging_setup import configure_logging


@pytest.fixture
def isolated_audit_logger():
    """Snapshot the orca.audit logger's handlers + propagate flag, restore
    after the test so other tests are not polluted by file handles."""
    logger = logging.getLogger("orca.audit")
    saved_handlers = list(logger.handlers)
    saved_propagate = logger.propagate
    yield logger
    for h in logger.handlers:
        if h not in saved_handlers:
            h.close()
            logger.removeHandler(h)
    logger.propagate = saved_propagate


def test_audit_log_handler_is_wired(
    tmp_path: Path, isolated_audit_logger: logging.Logger,
) -> None:
    configure_logging(tmp_path)

    isolated_audit_logger.info(
        "thread.skip_method level=CRITICAL reason=%r args=%r",
        "test reason", {"thread_id": "t1"},
    )

    # Force-flush so we can read.
    for h in isolated_audit_logger.handlers:
        h.flush()

    audit_file = tmp_path / "orca_audit.log"
    assert audit_file.exists()
    contents = audit_file.read_text(encoding="utf-8")
    assert "thread.skip_method" in contents
    assert "test reason" in contents


def test_audit_log_does_not_propagate_to_orca_root(
    tmp_path: Path, isolated_audit_logger: logging.Logger,
) -> None:
    """Audit entries should NOT double-write to orca.log."""
    configure_logging(tmp_path)

    isolated_audit_logger.info("audit-only message marker-XYZ")

    for h in isolated_audit_logger.handlers:
        h.flush()
    for h in logging.getLogger("orca").handlers:
        h.flush()

    audit_contents = (tmp_path / "orca_audit.log").read_text(encoding="utf-8")
    main_contents = (tmp_path / "orca.log").read_text(encoding="utf-8")

    assert "marker-XYZ" in audit_contents
    assert "marker-XYZ" not in main_contents
