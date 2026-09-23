"""A control scope that resolves without a built runtime.

Batch dispatch goes through ``orca.gateway.adhoc.operator_control``, which
refuses when the runtime is not built -- correct in production, and not what a
unit test over the executor's own behaviour is pinning. This stubs the
resolution and keeps the real controller, so the gate under test is the one the
test names.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest

from orca.gateway.adhoc import OperatorControl
from orca.runtime.run_modes import WorkflowRunMode


def stub_control_scope(
    monkeypatch: pytest.MonkeyPatch, fallback_controller: object,
) -> None:
    """Point ``orca.gateway.batch``'s scope at a resolved, un-flagged device."""

    @asynccontextmanager
    async def _scope(
        runtime: object, device_id: str, controller: object = None,
    ) -> AsyncIterator[OperatorControl]:
        device = MagicMock()
        device.interfaces = []
        yield OperatorControl(
            None, device_id, device, WorkflowRunMode.LIVE,
            controller if controller is not None else fallback_controller,
        )

    monkeypatch.setattr("orca.gateway.batch.operator_control", _scope)
