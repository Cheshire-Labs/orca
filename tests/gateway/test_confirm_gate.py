"""A vendor command flagged requires_confirm dispatches only with an explicit confirm.

The vendor passthrough advertises the whole controller surface of a wrapped
backend (~100 commands on a PreciseFlex, including raw `send_command`), and
the catalog flags everything the driver did not declare read-safe. The
controller is the single dispatch chokepoint, so the acknowledgement is
enforced here: a flagged command without ``confirm=True`` is refused before
any wire send, whatever surface it came in on.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from cheshire_drivers.driver_introspection import MethodInfo

from orca.gateway.batch import BatchExecutor, DeviceCommand
from tests.gateway._control_scope import stub_control_scope
from orca.gateway.controller import DeviceController
from orca.gateway.controller.controller import connection_manager
from orca.gateway.controller.exceptions import (
    CommandTimeoutError,
    ConfirmationRequiredError,
)
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.runtime.run_modes import WorkflowRunMode


def _snapshot_with_vendor_extras(device_id: str) -> DeviceSnapshot:
    return DeviceSnapshot(
        type="transporter",
        name=device_id,
        interfaces=["IShaker"],
        capabilities=["set_power", "request_power_state"],
        provides_state=False,
        methods={
            "set_power": MethodInfo(
                kind="method", params={}, requires_confirm=True,
            ),
            "request_power_state": MethodInfo(
                kind="method", params={}, requires_confirm=False,
            ),
        },
        site="test-site",
        lab="test-lab",
        workcell=None,
        status="ready",
        last_seen=datetime.utcnow(),
    )


@pytest.fixture
def controller() -> DeviceController:
    return DeviceController()


@pytest.mark.asyncio
class TestConfirmGate:
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_flagged_command_without_confirm_is_refused(
        self, mock_registry, controller,
    ):
        """Refusal happens in validation: no reservation, no wire send."""
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot_with_vendor_extras("pf400_1"),
        )

        with pytest.raises(ConfirmationRequiredError, match="set_power"):
            await controller.execute_command(
                "pf400_1", "set_power", {"enable": False},
                effective_mode=WorkflowRunMode.LIVE,
            )

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_flagged_command_with_confirm_reaches_dispatch(
        self, mock_registry, controller,
    ):
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot_with_vendor_extras("pf400_1"),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")

        with patch.object(
            connection_manager, "send_to_client", AsyncMock(return_value=True),
        ):
            # Timing out while awaiting the (never-sent) response proves the
            # gate passed; refusal would raise before the wire stage.
            with pytest.raises(CommandTimeoutError):
                await controller.execute_command(
                    "pf400_1", "set_power", {"enable": False},
                    timeout_seconds=0.05,
                    effective_mode=WorkflowRunMode.LIVE,
                    confirm=True,
                )

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_read_safe_command_needs_no_confirm(
        self, mock_registry, controller,
    ):
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot_with_vendor_extras("pf400_1"),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")

        with patch.object(
            connection_manager, "send_to_client", AsyncMock(return_value=True),
        ):
            with pytest.raises(CommandTimeoutError):
                await controller.execute_command(
                    "pf400_1", "request_power_state", {},
                    timeout_seconds=0.05,
                    effective_mode=WorkflowRunMode.LIVE,
                )

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_command_missing_from_the_catalog_is_not_gated(
        self, mock_registry, controller,
    ):
        """A device advertising capabilities but no method metadata (older
        device bridge) keeps its pre-gate behavior; the gate never guesses."""
        snapshot = _snapshot_with_vendor_extras("pf400_1")
        snapshot.methods = {}
        mock_registry.get_device = AsyncMock(return_value=snapshot)
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")

        with patch.object(
            connection_manager, "send_to_client", AsyncMock(return_value=True),
        ):
            with pytest.raises(CommandTimeoutError):
                await controller.execute_command(
                    "pf400_1", "set_power", {"enable": False},
                    timeout_seconds=0.05,
                    effective_mode=WorkflowRunMode.LIVE,
                )


@pytest.mark.asyncio
class TestFlaggedCommandResendPolicy:
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_a_flagged_command_never_resends_on_reconnect(
        self, mock_registry, controller,
    ):
        """A resend would re-drive hardware of unknown idempotency and skip
        the audit emission, so a flagged command fails offline instead."""
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot_with_vendor_extras("pf400_1"),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")

        with patch.object(
            connection_manager, "send_to_client", AsyncMock(return_value=True),
        ):
            task = asyncio.create_task(
                controller.execute_command(
                    "pf400_1", "set_power", {"enable": False},
                    timeout_seconds=0.2,
                    effective_mode=WorkflowRunMode.LIVE,
                    confirm=True,
                )
            )
            while "pf400_1" not in controller._pending:
                await asyncio.sleep(0)
            assert controller._pending["pf400_1"].resend_on_reconnect is False
            with pytest.raises(CommandTimeoutError):
                await task


@pytest.mark.asyncio
class TestConfirmAudit:
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_confirmed_dangerous_send_lands_on_the_audit_logger(
        self, mock_registry, controller, caplog,
    ):
        """The send is the auditable fact: it is recorded even when the
        response later times out, because the device may have acted."""
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot_with_vendor_extras("pf400_1"),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")

        with patch.object(
            connection_manager, "send_to_client", AsyncMock(return_value=True),
        ):
            with caplog.at_level("INFO", logger="orca.audit"):
                with pytest.raises(CommandTimeoutError):
                    await controller.execute_command(
                        "pf400_1", "set_power", {"enable": False},
                        timeout_seconds=0.05,
                        effective_mode=WorkflowRunMode.LIVE,
                        confirm=True,
                    )

        audit = [r for r in caplog.records if r.name == "orca.audit"]
        assert len(audit) == 1
        action_name, danger_level, reason, call_args = audit[0].args
        assert action_name == "device.set_power"
        assert danger_level == "PHYSICAL"
        assert reason is None
        assert call_args == {
            "device_id": "pf400_1", "params": {"enable": False},
        }

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_read_safe_send_is_not_audited(
        self, mock_registry, controller, caplog,
    ):
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot_with_vendor_extras("pf400_1"),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")

        with patch.object(
            connection_manager, "send_to_client", AsyncMock(return_value=True),
        ):
            with caplog.at_level("INFO", logger="orca.audit"):
                with pytest.raises(CommandTimeoutError):
                    await controller.execute_command(
                        "pf400_1", "request_power_state", {},
                        timeout_seconds=0.05,
                        effective_mode=WorkflowRunMode.LIVE,
                    )

        assert not [r for r in caplog.records if r.name == "orca.audit"]


@pytest.mark.asyncio
class TestBatchConfirm:
    @patch("orca.gateway.adhoc.device_connection_tracker")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_batch_refusal_is_a_command_failure_not_a_crash(
        self, mock_registry, adhoc_registry, monkeypatch: pytest.MonkeyPatch,
    ):
        """The batch reports the refusal per-command (no retry: a missing
        confirm never heals on its own) instead of unwinding the whole batch."""
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot_with_vendor_extras("pf400_1"),
        )
        adhoc_registry.get_device = mock_registry.get_device
        controller = DeviceController()
        stub_control_scope(monkeypatch, controller)

        executor = BatchExecutor(controller, lambda _: WorkflowRunMode.LIVE)
        result = await executor.execute_batch(
            [DeviceCommand(device_id="pf400_1", command="set_power",
                           params={"enable": False})],
        )

        assert result.success is False
        assert result.results[0].error_type == "ConfirmationRequiredError"
        assert "confirm" in (result.results[0].error or "")

    @patch("orca.gateway.adhoc.device_connection_tracker")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_batch_carries_confirm_through_to_the_controller(
        self, mock_registry, adhoc_registry, monkeypatch: pytest.MonkeyPatch,
    ):
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot_with_vendor_extras("pf400_1"),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        adhoc_registry.get_device = mock_registry.get_device
        controller = DeviceController()
        stub_control_scope(monkeypatch, controller)

        executor = BatchExecutor(controller, lambda _: WorkflowRunMode.LIVE)
        with patch.object(
            connection_manager, "send_to_client", AsyncMock(return_value=True),
        ):
            result = await executor.execute_batch(
                [DeviceCommand(device_id="pf400_1", command="set_power",
                               params={"enable": False}, timeout=0.05,
                               confirm=True)],
            )

        assert result.results[0].error_type == "CommandTimeoutError"
