"""CLI and daemon tests for list verb scoping.

Covers:
- ``orca teachpoints list`` (no args) -> list_all
- ``orca teachpoints list <device_id>`` -> per-device (backward compat)
- ``orca deck-layouts list`` (no args) -> list_all
- ``orca reservation list`` defaults to latest execution (one-line hint),
  including the stale-recorded and no-record exit paths
- ``orca reservation list --all`` spans every execution
- ``orca reservation list --execution X --all`` is rejected as mutually
  exclusive (Pydantic-style guard on the CLI verb itself)
- ``orca var list`` defaults to latest execution (one-line hint),
  including the stale-recorded and no-record exit paths
- ``orca var list --all`` spans every execution
- Daemon ``GET /teachpoints`` and ``GET /deck-layouts`` aggregate across
  transporters / liquid handlers
"""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.daemon.schemas import (
    CrossExecVariableDTO,
    DeckLayoutSummaryDTO,
    ReservationSnapshotDTO,
    TeachpointDTO,
)


runner = CliRunner()


def _tp(
    *, device_id: str, position_id: str,
) -> TeachpointDTO:
    return TeachpointDTO(
        device_id=device_id,
        position_id=position_id,
        coord_type="cartesian",
        coords={"type": "cartesian", "x": 0.0, "y": 0.0, "z": 0.0},
        access_config_name=None,
        orientation=None,
        gateway=None,
    )


def _layout(*, device_id: str, name: str) -> DeckLayoutSummaryDTO:
    return DeckLayoutSummaryDTO(
        device_id=device_id, name=name, deck_type="STAR",
    )


class TestTeachpointsListVerb:
    def test_no_arg_calls_list_all(self) -> None:
        with patch("orca.cli.teachpoint.get_client") as mock_local:
            mock_local.return_value.teachpoints_list_all.return_value = [
                _tp(device_id="robot1", position_id="pad1"),
                _tp(device_id="robot2", position_id="shaker1"),
            ]
            result = runner.invoke(app, ["teachpoints", "list"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.teachpoints_list_all.assert_called_once()
        mock_local.return_value.teachpoints_list.assert_not_called()
        assert "robot1" in result.output
        assert "robot2" in result.output
        assert "all" in result.output.lower()

    def test_device_arg_preserves_legacy_per_device_call(self) -> None:
        with patch("orca.cli.teachpoint.get_client") as mock_local:
            mock_local.return_value.teachpoints_list.return_value = [
                _tp(device_id="robot1", position_id="pad1"),
            ]
            result = runner.invoke(app, ["teachpoints", "list", "robot1"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.teachpoints_list.assert_called_once_with("robot1")
        mock_local.return_value.teachpoints_list_all.assert_not_called()


class TestDeckLayoutsListVerb:
    def test_no_arg_calls_list_all(self) -> None:
        with patch("orca.cli.deck_layout.get_client") as mock_local:
            mock_local.return_value.deck_layouts_list_all.return_value = [
                _layout(device_id="lh1", name="layoutA"),
                _layout(device_id="lh2", name="layoutB"),
            ]
            result = runner.invoke(app, ["deck-layouts", "list"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.deck_layouts_list_all.assert_called_once()
        mock_local.return_value.deck_layouts_list.assert_not_called()
        assert "lh1" in result.output
        assert "lh2" in result.output

    def test_device_arg_preserves_legacy_per_device_call(self) -> None:
        with patch("orca.cli.deck_layout.get_client") as mock_local:
            mock_local.return_value.deck_layouts_list.return_value = [
                _layout(device_id="lh1", name="layoutA"),
            ]
            result = runner.invoke(app, ["deck-layouts", "list", "lh1"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.deck_layouts_list.assert_called_once_with("lh1")
        mock_local.return_value.deck_layouts_list_all.assert_not_called()


class TestReservationListMutex:
    def test_all_with_execution_rejected_as_mutex(self) -> None:
        with patch("orca.cli.reservation.get_client"):
            result = runner.invoke(
                app, ["reservation", "list", "--all", "--execution", "exec-1"],
            )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()


class TestVariablesListVerb:
    def test_all_calls_cross_exec_endpoint(self) -> None:
        rows = [
            CrossExecVariableDTO(execution_id="exec-A123", name="x", value=1),
            CrossExecVariableDTO(
                execution_id="exec-B456", name="y", value="hello",
            ),
        ]
        with patch("orca.cli.var.get_client") as mock_local:
            mock_local.return_value.variables_list_all.return_value = rows
            result = runner.invoke(app, ["var", "list", "--all"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.variables_list_all.assert_called_once()
        assert "exec-A12" in result.output
        assert "exec-B45" in result.output
        assert "hello" in result.output

    def test_all_with_execution_rejected_as_mutex(self) -> None:
        with patch("orca.cli.var.get_client"):
            result = runner.invoke(
                app, ["var", "list", "--all", "--execution", "exec-1"],
            )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()


class TestReservationsListVerb:
    def test_all_calls_cross_exec_endpoint(self) -> None:
        rows = [
            ReservationSnapshotDTO(
                position_id="pad1",
                reservation_id="rsv-aaaa-1111",
                thread_id="thr-bbbb-2222",
                execution_id="exec-A1234567",
            ),
        ]
        with patch("orca.cli.reservation.get_client") as mock_local:
            mock_local.return_value.reservations_list_all.return_value = rows
            result = runner.invoke(app, ["reservation", "list", "--all"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.reservations_list_all.assert_called_once()
        assert "exec-A12" in result.output
        assert "rsv-aaaa" in result.output
        assert "pad1" in result.output


class _StubExec:
    def __init__(self, eid: str) -> None:
        self.id = eid


class TestReservationListDefaultsToLastExecution:
    """`orca reservation list` with no flags defaults to the recorded
    `last` execution, emits a one-line hint to stderr, and forwards the
    resolved id to the per-execution endpoint. Stale-recorded and
    no-record cases must exit nonzero with the typed messages.
    """

    def test_happy_path_resolves_last_and_emits_hint(self) -> None:
        with patch("orca.cli.reservation.get_client") as mock_local, \
                patch(
                    "orca.cli.reservation.resolve.get_last_execution_id",
                    return_value="exec-A1234567",
                ):
            mock_local.return_value.list_executions.return_value = [
                _StubExec("exec-A1234567"),
            ]
            mock_local.return_value.list_reservations.return_value = []
            result = runner.invoke(app, ["reservation", "list"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.list_reservations.assert_called_once_with(
            "exec-A1234567",
        )
        assert "using latest execution exec-A12" in result.output

    def test_stale_recorded_last_fails_with_typed_message(self) -> None:
        with patch("orca.cli.reservation.get_client") as mock_local, \
                patch(
                    "orca.cli.reservation.resolve.get_last_execution_id",
                    return_value="exec-STALE000",
                ):
            mock_local.return_value.list_executions.return_value = [
                _StubExec("exec-FRESH111"),
            ]
            result = runner.invoke(app, ["reservation", "list"])
        assert result.exit_code != 0
        assert "no longer known to runtime" in result.output
        mock_local.return_value.list_reservations.assert_not_called()

    def test_no_last_recorded_fails_with_usage_exit(self) -> None:
        with patch("orca.cli.reservation.get_client") as mock_local, \
                patch(
                    "orca.cli.reservation.resolve.get_last_execution_id",
                    return_value=None,
                ):
            mock_local.return_value.list_executions.return_value = []
            result = runner.invoke(app, ["reservation", "list"])
        from orca.cli.output import EXIT_USAGE
        assert result.exit_code == EXIT_USAGE
        assert "no 'last' execution recorded" in result.output
        mock_local.return_value.list_reservations.assert_not_called()


class TestVarListDefaultsToLastExecution:
    """`orca var list` with no flags defaults to the recorded `last`
    execution, emits a one-line hint, and forwards the resolved id to
    the per-execution endpoint. Stale-recorded and no-record cases
    exit nonzero with the typed messages.
    """

    def test_happy_path_resolves_last_and_emits_hint(self) -> None:
        with patch("orca.cli.var.get_client") as mock_local, \
                patch(
                    "orca.cli.var.resolve.get_last_execution_id",
                    return_value="exec-A1234567",
                ):
            mock_local.return_value.list_executions.return_value = [
                _StubExec("exec-A1234567"),
            ]
            mock_local.return_value.variables_list.return_value = {"x": 1}
            result = runner.invoke(app, ["var", "list"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.variables_list.assert_called_once_with(
            "exec-A1234567",
        )
        assert "using latest execution exec-A12" in result.output

    def test_stale_recorded_last_fails_with_typed_message(self) -> None:
        with patch("orca.cli.var.get_client") as mock_local, \
                patch(
                    "orca.cli.var.resolve.get_last_execution_id",
                    return_value="exec-STALE000",
                ):
            mock_local.return_value.list_executions.return_value = [
                _StubExec("exec-FRESH111"),
            ]
            result = runner.invoke(app, ["var", "list"])
        assert result.exit_code != 0
        assert "no longer known to runtime" in result.output
        mock_local.return_value.variables_list.assert_not_called()

    def test_no_last_recorded_fails_with_usage_exit(self) -> None:
        with patch("orca.cli.var.get_client") as mock_local, \
                patch(
                    "orca.cli.var.resolve.get_last_execution_id",
                    return_value=None,
                ):
            mock_local.return_value.list_executions.return_value = []
            result = runner.invoke(app, ["var", "list"])
        from orca.cli.output import EXIT_USAGE
        assert result.exit_code == EXIT_USAGE
        assert "no 'last' execution recorded" in result.output
        mock_local.return_value.variables_list.assert_not_called()


class TestDaemonNoArgRoutesAreReachable:
    """Daemon-level smoke check: the new no-arg list routes resolve and
    return 200 (empty bodies on a fresh runtime). Pre-fix, route
    ordering in ``routes.py`` could silently shadow these as
    ``device_id=""`` paths -- this test would catch that drift.
    """

    @pytest.mark.asyncio
    async def test_teachpoints_and_deck_layouts_and_reservations_and_variables(
        self,
    ) -> None:
        from httpx import ASGITransport, AsyncClient

        from orca.daemon.app import create_app
        from orca.runtime.system_runtime import SystemRuntime
        from tests.test_system_runtime import _build_simple_system

        system, _ = await _build_simple_system()
        rt = SystemRuntime(system)
        await rt.start()
        app_obj = create_app(initial_system_runtime=rt)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app_obj),
                base_url="http://daemon.test",
            ) as c:
                for path in (
                    "/teachpoints", "/deck-layouts",
                    "/reservations", "/variables",
                ):
                    resp = await c.get(path)
                    assert resp.status_code == 200, (
                        f"GET {path} -> {resp.status_code} {resp.text!r}"
                    )
                    assert isinstance(resp.json(), list)
        finally:
            if rt.state.name == "RUNNING":
                await rt.shutdown(confirm=True)


class TestCrossExecDTORoundTrip:
    def test_cross_exec_variable_dto_round_trips(self) -> None:
        dto = CrossExecVariableDTO(
            execution_id="exec-abc", name="x", value=42,
        )
        dumped = dto.model_dump(mode="json")
        reloaded = CrossExecVariableDTO.model_validate(dumped)
        assert reloaded.execution_id == "exec-abc"
        assert reloaded.name == "x"
        assert reloaded.value == 42

    def test_reservation_snapshot_dto_execution_id_defaults_none(self) -> None:
        """Backward compat: per-execution route does not populate
        execution_id (the URL already carries it). The cross-exec
        route populates it. Default None ensures existing test fixtures
        and per-execution responses still validate.
        """
        from orca.daemon.schemas import ReservationSnapshotDTO

        dto = ReservationSnapshotDTO(
            position_id="pad1",
            reservation_id="rsv-1",
            thread_id="thr-1",
        )
        assert dto.execution_id is None
