"""The verbs and routes that start, mount, reload, unload and stop the daemon, run in this order on one child daemon."""

from tests.surface.cli_cases import CliCase, V
from tests.surface.rest_cases import R, RestCase

LAST = "$last"
PORT = "$port"

LIFECYCLE_STEPS: list[CliCase | RestCase] = [
    V("start", "--port", PORT, expect="daemon started", world="process"),
    V("status", expect="daemon", world="process"),
    V("topology mount", expect="mounted", world="process"),
    V("runtime reload", "--reason", "surface suite", expect="reloaded", world="process"),
    R("POST", "/api/runtime/reload", 200, "registered", world="process"),
    V("run", "learn_orca", "--run-mode", "CHECK_RUN", "--wait", expect="submitted", world="process"),
    V("labware check-run", LAST, expect="Check run", world="process"),
    R("GET", "/api/executions/{execution_id}/check-run-labware", 200, "labware",
      path=f"/api/executions/{LAST}/check-run-labware", world="process"),
    V("unload", expect="unloaded", world="process"),
    R("POST", "/api/runtime/mount-package", 200, "runtime_state", world="process"),
    R("POST", "/api/runtime/unload", 200, "status", world="process"),
    V("shutdown", expect="daemon stopped", world="process"),
    V("start", "--port", PORT, expect="daemon started", world="process"),
    R("POST", "/shutdown", 200, "status", world="process"),
]

LIFECYCLE_CLI_CASES = [s for s in LIFECYCLE_STEPS if isinstance(s, CliCase)]
LIFECYCLE_REST_CASES = [s for s in LIFECYCLE_STEPS if isinstance(s, RestCase)]
