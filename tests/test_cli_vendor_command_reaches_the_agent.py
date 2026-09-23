"""`orca device send` puts a vendor command on a real device bridge's socket.

The facade tests check the facade against a double of the gateway. This one
takes the whole path an operator's keystroke travels: the CLI, HTTP to a
daemon running in its own process, the device facade, the capability gate, the
controller, and a WebSocket that a device bridge is genuinely connected to. The
device bridge here is a socket this test holds open, so what it receives is
what an on-prem orca-client would have received.

What that device bridge then does with the name is orca-client's
`test_vendor_command_reaches_the_robot`, where a real Flex driver turns
`gripper.ungrip` into the robot's `unsafe/ungripLabware`.
"""

import asyncio
import json
import threading
from typing import Any, Dict, List, Optional

import pytest
import websockets
from typer.testing import CliRunner

from cheshire_drivers.gateway_protocol import PROTOCOL_VERSION
from orca.cli.app import app


try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()


DEVICE = "flex_vendor_1"

CONNECT = {
    "type": "connect",
    "payload": {
        "protocol_version": PROTOCOL_VERSION,
        "site": "vendor",
        "lab": "surface",
        "workcell": None,
        "devices": [
            {
                "type": "liquid_handler",
                "name": DEVICE,
                "interfaces": ["ILiquidHandler", "IForceGripperJaw"],
                "capabilities": ["gripper.ungrip", "gripper.grip"],
                "provides_state": True,
                "methods": {},
            }
        ],
    },
}


class _Agent:
    """A device bridge on the far end of the daemon's /ws/devices socket.

    Runs its own event loop on a thread, because the CLI under test is
    synchronous: it blocks the calling thread on an HTTP request that cannot
    return until this device bridge has answered.
    """

    def __init__(self, port: int) -> None:
        self._url = f"ws://127.0.0.1:{port}/ws/devices"
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._stop = False
        self.commands: List[Dict[str, Any]] = []
        self.error: Optional[BaseException] = None

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as exc:
            self.error = exc
            self._ready.set()

    async def _serve(self) -> None:
        async with websockets.connect(self._url) as socket:
            await socket.send(json.dumps(CONNECT))
            self._ready.set()
            while not self._stop:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                envelope = json.loads(raw)
                if envelope.get("type") != "command":
                    continue
                payload = envelope["payload"]
                self.commands.append(
                    {"command": payload["command"], "params": payload.get("params", {})}
                )
                await socket.send(json.dumps({
                    "type": "response",
                    "payload": {
                        "command_id": payload["command_id"],
                        "success": True,
                        "result": None,
                    },
                }))

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(timeout=10), "device bridge never connected"
        if self.error is not None:
            raise self.error

    def stop(self) -> None:
        self._stop = True
        self._thread.join(timeout=5)

    @property
    def names(self) -> List[str]:
        return [c["command"] for c in self.commands]


@pytest.fixture
def agent(loaded_daemon):
    """A device bridge connected to the daemon, advertising a Flex gripper.

    The daemon needs a system mounted before it answers device requests at
    all; the gripper's device is not in that topology, which is the ordinary
    case of a device bridge connecting with hardware the topology never
    declared.
    """
    connected = _Agent(loaded_daemon.port)
    connected.start()
    yield connected
    connected.stop()


def test_cli_send_puts_the_prefixed_name_on_the_agents_socket(agent) -> None:
    """This is the whole path an operator uses to open the jaws. Any layer that
    trimmed the prefix would put `ungrip` on this socket, and the robot has no
    such command."""
    result = runner.invoke(app, ["--json", "device", "send", DEVICE, "gripper.ungrip"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert agent.names == ["gripper.ungrip"]


def test_cli_send_carries_the_arguments(agent) -> None:
    """A name that arrives without its arguments grips at the robot's default
    force rather than the one asked for."""
    result = runner.invoke(
        app, ["--json", "device", "send", DEVICE, "gripper.grip", "force=12.0"],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert agent.commands == [{"command": "gripper.grip", "params": {"force": 12.0}}]


def test_cli_invoke_reaches_the_same_command(agent) -> None:
    """`invoke` and `send` are both operator entry points for one command, and
    an operator who reaches for the other one must not be refused."""
    result = runner.invoke(app, ["--json", "device", "invoke", DEVICE, "gripper.ungrip"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert agent.names == ["gripper.ungrip"]


def test_cli_lists_the_command_it_will_accept(agent) -> None:
    """An operator finds the name here before sending it. A listing that omits
    what the gate accepts hides the command; one that adds to it invites a
    refusal."""
    result = runner.invoke(app, ["--json", "device", "capabilities", DEVICE])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "gripper.ungrip" in json.loads(result.stdout)["capabilities"]


def test_cli_refuses_a_command_the_agent_never_advertised(agent) -> None:
    """The advertised set is the gate, and a name outside it must stop here
    rather than travel to hardware to be refused."""
    result = runner.invoke(app, ["device", "send", DEVICE, "gripper.detonate"])

    assert result.exit_code != 0
    assert agent.names == []
