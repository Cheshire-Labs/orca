"""`orca device registry` names the driver behind `device_connected`.

The device bridge holds one driver per run mode and reports a link for each. A
registry read names no mode, so it shows the one link worth showing plus which
driver it belongs to. Without the mode an operator reading "connected" on a
DEVICE_SIM bench takes a simulator's open link for the instrument's.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.control_plane import DeviceRegistryEntryDTO


runner = CliRunner()


def _entry(
    *,
    device_link_mode: str | None,
    agent_backed: bool = True,
    linked: bool = True,
) -> DeviceRegistryEntryDTO:
    connection_card = None
    if agent_backed and device_link_mode is not None:
        connection_card = {
            "name": "pf400_1",
            "advertised_kind": "transporter",
            "device_is_connected": True,
            "device_is_initialized": True,
            "device_link_mode": device_link_mode,
        }
    return DeviceRegistryEntryDTO.model_validate({
        "name": "pf400_1",
        "topology_card": {"declared_kind": "transporter"},
        "connection_card": connection_card,
        "is_client_connected": agent_backed,
        "is_device_connected": linked,
        "is_initialized": linked,
        "device_link_mode": device_link_mode,
        "mode_eligibility": {
            "pure_sim": True, "device_sim": True, "live": True,
        },
    })


def test_show_names_the_simulator_behind_an_open_link() -> None:
    with patch("orca.cli.device.get_client") as client:
        client.return_value.device_registry_show.return_value = _entry(
            device_link_mode="DEVICE_SIM",
        )
        result = runner.invoke(app, ["device", "registry", "show", "pf400_1"])
    assert result.exit_code == 0, result.output
    assert "DEVICE_SIM" in result.output


def test_list_names_the_driver_that_answered() -> None:
    with patch("orca.cli.device.get_client") as client:
        client.return_value.device_registry_list.return_value = [
            _entry(device_link_mode="LIVE"),
        ]
        result = runner.invoke(app, ["device", "registry", "list"])
    assert result.exit_code == 0, result.output
    assert "LIVE" in result.output


def test_a_daemon_bench_with_no_agent_still_names_the_world_it_answered_for()  -> None:
    """With no device bridge there is no card, so the mode has to ride the
    entry itself.

    This is the daemon path, where the flags describe orca's own simulator
    because nothing seeds a run mode. Losing the label here would show a
    simulator's open link as the instrument on exactly the deployment where
    the operator has no device bridge log to check it against.
    """
    with patch("orca.cli.device.get_client") as client:
        client.return_value.device_registry_show.return_value = _entry(
            device_link_mode="PURE_SIM", agent_backed=False,
        )
        result = runner.invoke(app, ["device", "registry", "show", "pf400_1"])
    assert result.exit_code == 0, result.output
    assert "PURE_SIM" in result.output


def test_nobody_could_answer_so_no_mode_is_claimed() -> None:
    """An agent-held device whose agent went quiet has no mode to report.

    Unknown forces both flags False, so this is the shape the server actually
    emits for it, not just a null mode bolted onto a connected device.
    """
    with patch("orca.cli.device.get_client") as client:
        client.return_value.device_registry_show.return_value = _entry(
            device_link_mode=None, agent_backed=False, linked=False,
        )
        result = runner.invoke(app, ["device", "registry", "show", "pf400_1"])
    assert result.exit_code == 0, result.output
    assert "device_link_mode: -" in result.output
    assert "device_connected: False" in result.output
