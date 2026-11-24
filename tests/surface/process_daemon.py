"""A daemon started and stopped by the `orca` CLI in child processes, for the verbs that start, mount and stop it."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import psutil
from pydantic import JsonValue

from orca.daemon.lifecycle import read_pid_file
from tests.surface.context import as_dict, as_list
from tests.surface.world import EXAMPLES_ROOT, CliReply, Reply

CALL_LIMIT_S = 300.0
# A step may look up the last run, then make one call.
STEP_LIMIT_S = 2 * CALL_LIMIT_S


class ProcessDaemon:
    def __init__(self, root: Path, package: str) -> None:
        self.folder = root / package
        shutil.copytree(EXAMPLES_ROOT / package, self.folder, ignore=shutil.ignore_patterns(".orca", "__pycache__"))
        self.home = root / "home"
        self.home.mkdir()
        project = Path(__file__).resolve().parents[2]
        self._env = {
            **os.environ,
            "ORCA_DAEMON_HOME": str(self.home),
            "ORCA_PACKAGE_DIR": str(self.folder),
            "PYTHONPATH": os.pathsep.join([str(project / "src"), str(project), os.environ.get("PYTHONPATH", "")]),
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        }

    def cli(self, *args: str) -> CliReply:
        done = subprocess.run(
            [sys.executable, "-m", "orca.cli", *args], env=self._env, capture_output=True, timeout=CALL_LIMIT_S,
            stdin=subprocess.DEVNULL,
        )
        return CliReply(done.returncode, (done.stdout + done.stderr).decode("utf-8", errors="replace"))

    def request(self, method: str, path: str, *, json: JsonValue = None) -> Reply:
        info = read_pid_file(self.home / "daemon.json")
        assert info is not None, "no daemon is recorded"
        response = httpx.request(method, f"http://127.0.0.1:{info.port}{path}", json=json, timeout=CALL_LIMIT_S)
        try:
            body: JsonValue = response.json()
        except ValueError:
            body = None
        return Reply(response.status_code, body, response.text)

    def kill(self) -> None:
        info = read_pid_file(self.home / "daemon.json")
        if info is not None and psutil.pid_exists(info.pid):
            psutil.Process(info.pid).kill()

    def last_execution_id(self) -> str:
        executions = as_list(as_dict(self.request("GET", "/api/executions").body)["executions"])
        return str(as_dict(executions[-1])["id"])
