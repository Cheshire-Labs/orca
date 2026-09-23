"""Logging configuration for Orca CLI sessions.

Sets up four log handlers:
- orca.log: all orca.* loggers at INFO+
- orca_events.jsonl: structured event log from orca.events
- orca_alerts.log: PAUSED/error alerts from orca.alerts
- orca_audit.log: every confirmed @dangerous operation from orca.audit.
  Rotating: persists across daemon restarts so the audit trail survives.
"""

import logging
import logging.handlers
import subprocess
import sys
from pathlib import Path

_AUDIT_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB per file
_AUDIT_LOG_BACKUP_COUNT = 5  # keep 5 rotated archives -> 60 MiB ceiling


def configure_logging(log_dir: Path) -> None:
    """Configure file-based logging for an Orca CLI session."""
    log_dir.mkdir(parents=True, exist_ok=True)

    # Main log: orca.* at INFO+
    main_handler = logging.FileHandler(log_dir / "orca.log", mode="a")
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger("orca").addHandler(main_handler)

    # Events log: structured JSON lines
    events_handler = logging.FileHandler(log_dir / "orca_events.jsonl", mode="a")
    events_handler.setLevel(logging.INFO)
    events_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("orca.events").addHandler(events_handler)

    # Alerts log: PAUSED/error events
    alerts_handler = logging.FileHandler(log_dir / "orca_alerts.log", mode="a")
    alerts_handler.setLevel(logging.ERROR)
    alerts_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logging.getLogger("orca.alerts").addHandler(alerts_handler)

    # Audit log: every confirmed @dangerous operation. The danger
    # decorator emits via `audit_logger.info(...)` on every recorded
    # AuditEntry; without a handler that record dies in the in-memory
    # ring buffer (1000-entry cap, lost on restart). Rotating handler
    # so a long-running daemon does not unbounded-grow the audit file.
    audit_handler = logging.handlers.RotatingFileHandler(
        log_dir / "orca_audit.log",
        mode="a",
        maxBytes=_AUDIT_LOG_MAX_BYTES,
        backupCount=_AUDIT_LOG_BACKUP_COUNT,
    )
    audit_handler.setLevel(logging.INFO)
    audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    audit_logger = logging.getLogger("orca.audit")
    audit_logger.addHandler(audit_handler)
    # Do not propagate to the orca.* root handler -- audit entries
    # already live in their own file, propagation would double-write
    # them into orca.log.
    audit_logger.propagate = False


def spawn_alert_window(log_dir: Path) -> subprocess.Popen[bytes] | None:
    """Spawn a terminal window tailing the alerts log (interactive mode only).

    Returns the process handle, or None if not interactive or on failure.
    """
    if not sys.stdin.isatty():
        return None

    alerts_path = log_dir / "orca_alerts.log"
    alerts_path.touch(exist_ok=True)

    try:
        if sys.platform == "win32":
            proc = subprocess.Popen(
                ["cmd", "/c", "start", "Orca Alerts", "powershell", "-Command",
                 f"Get-Content '{alerts_path}' -Wait"],
            )
        else:
            proc = subprocess.Popen(
                ["tail", "-f", str(alerts_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return proc
    except OSError:
        return None
