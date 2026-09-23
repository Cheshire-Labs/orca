"""Operation-level tests for the manual-step surface.

`ConfirmManualStepOperation` maps the runtime's KeyError to a not-found
OperationError carrying step_id + execution_id in extras;
`ListPendingManualStepsOperation` projects PendingManualStepRecord into
the wire DTO and only raises not-found when a specific (unknown) execution
was requested.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.manual_step import (
    ConfirmManualStepOperation,
    ListPendingManualStepsOperation,
)
from orca.operations.manual_step_models import (
    ConfirmManualStepRequest,
    ListPendingManualStepsRequest,
)
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.status_models import PendingManualStepRecord


class _FakeRuntime:
    """Records the two manual-step calls the operations make so tests can
    assert on them; `to_mock()` exposes it as a spec'd ISystemRuntime."""

    def __init__(
        self,
        records: list[PendingManualStepRecord] | None = None,
        confirm_raises: bool = False,
        list_raises: bool = False,
    ) -> None:
        self._records = records or []
        self._confirm_raises = confirm_raises
        self._list_raises = list_raises
        self.confirmed: tuple[str, str] | None = None

    def list_pending_manual_steps(
        self, execution_id: str | None = None,
    ) -> list[PendingManualStepRecord]:
        if self._list_raises:
            raise KeyError(execution_id)
        if execution_id is None:
            return self._records
        return [r for r in self._records if r.execution_id == execution_id]

    async def confirm_manual_step(self, execution_id: str, step_id: str) -> None:
        if self._confirm_raises:
            raise KeyError(step_id)
        self.confirmed = (execution_id, step_id)

    def to_mock(self) -> ISystemRuntime:
        runtime = MagicMock(spec=ISystemRuntime)
        runtime.list_pending_manual_steps = MagicMock(
            side_effect=self.list_pending_manual_steps,
        )
        runtime.confirm_manual_step = AsyncMock(
            side_effect=self.confirm_manual_step,
        )
        return runtime


def _record(execution_id: str, step_id: str) -> PendingManualStepRecord:
    return PendingManualStepRecord(
        execution_id=execution_id,
        step_id=step_id,
        instruction="do the thing",
        emitted_at=datetime.now(timezone.utc),
    )


class TestConfirmManualStepOperation:
    @pytest.mark.asyncio
    async def test_confirm_succeeds(self) -> None:
        runtime = _FakeRuntime()
        op = ConfirmManualStepOperation(runtime=runtime.to_mock())
        resp = await op.run(
            ConfirmManualStepRequest(execution_id="exec-a", step_id="manual_step-aaaa"),
        )
        assert resp.status == "confirmed"
        assert resp.execution_id == "exec-a"
        assert resp.step_id == "manual_step-aaaa"
        assert runtime.confirmed == ("exec-a", "manual_step-aaaa")

    @pytest.mark.asyncio
    async def test_not_found_maps_to_not_found_error(self) -> None:
        runtime = _FakeRuntime(confirm_raises=True)
        op = ConfirmManualStepOperation(runtime=runtime.to_mock())
        with pytest.raises(OperationError) as excinfo:
            await op.run(
                ConfirmManualStepRequest(
                    execution_id="exec-a", step_id="manual_step-zzzz",
                ),
            )
        err = excinfo.value
        assert err.code == OperationErrorCode.NOT_FOUND
        assert err.extras is not None
        assert err.extras["step_id"] == "manual_step-zzzz"
        assert err.extras["execution_id"] == "exec-a"


class TestListPendingManualStepsOperation:
    @pytest.mark.asyncio
    async def test_list_projects_records(self) -> None:
        runtime = _FakeRuntime(records=[
            _record("exec-a", "manual_step-aaaa"),
            _record("exec-b", "manual_step-bbbb"),
        ])
        op = ListPendingManualStepsOperation(runtime=runtime.to_mock())
        resp = await op.run(ListPendingManualStepsRequest(execution_id=None))
        assert {p.step_id for p in resp.pending} == {
            "manual_step-aaaa", "manual_step-bbbb",
        }
        assert resp.pending[0].instruction == "do the thing"

    @pytest.mark.asyncio
    async def test_unknown_execution_maps_to_not_found(self) -> None:
        runtime = _FakeRuntime(list_raises=True)
        op = ListPendingManualStepsOperation(runtime=runtime.to_mock())
        with pytest.raises(OperationError) as excinfo:
            await op.run(ListPendingManualStepsRequest(execution_id="nope"))
        assert excinfo.value.code == OperationErrorCode.NOT_FOUND
