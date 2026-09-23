"""Recovering a thread says the device its pause is about has been looked at.

A fault stands until someone says they checked the machine. An operator who
reconnects a dropped arm, initializes it and then retries the thread has said
exactly that, and on the bench nothing noticed: the retry was refused by the
fault the operator had already dealt with, and clearing it needed a second call
they had no reason to know about.

The aborts are the exception. They give up on the work and say nothing about
whether the jaws are empty.

Which fault a pause is about comes off the error, not off a device name. The
error that faulted a device carries the fault it left, and an error raised
because a fault was already standing carries that one, so a failed move and a
failed device call answer the same way.
"""

from typing import List
from unittest.mock import MagicMock, patch

import pytest

from orca.gateway.controller.controller import DeviceController
from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    DeviceFaultedError,
)
from orca.gateway.device_fault import DeviceFault, DeviceFaultOutcome
from orca.runtime.facades.threads import ThreadFacade
from orca.runtime import system_runtime
from orca.runtime.system_runtime import SystemRuntime, _fault_named_by_pause
from orca.workflow_models.status_enums import RecoveryDecision


def _fault(device_id: str = "pf400_1", command: str = "pick") -> DeviceFault:
    return DeviceFault(
        device_id=device_id, command=command, command_id=f"c-{command}",
        outcome=DeviceFaultOutcome.FAILED, error="Stall or Collision Detected",
        error_type="CommandExecutionError", at=0.0,
    )


class _Runtime:
    """The three runtime calls `recover` makes, and what it did with them."""

    def __init__(self, fault: DeviceFault | None) -> None:
        self._fault = fault
        self.cleared: List[DeviceFault] = []
        self.recovered: List[RecoveryDecision] = []

    def list_threads(self, execution_id: str) -> List[MagicMock]:
        return [MagicMock(id="t1")]

    def fault_named_by_pause(
        self, execution_id: str, thread_id: str,
    ) -> DeviceFault | None:
        return self._fault

    async def clear_fault_if_current(self, fault: DeviceFault) -> None:
        self.cleared.append(fault)

    def recover_thread(
        self, execution_id: str, thread_id: str, decision: RecoveryDecision,
    ) -> None:
        self.recovered.append(decision)


def _facade(fault: DeviceFault | None) -> tuple[ThreadFacade, _Runtime]:
    runtime = _Runtime(fault)
    return ThreadFacade(runtime, MagicMock()), runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [RecoveryDecision.RETRY, RecoveryDecision.RETRY_OP, RecoveryDecision.CONTINUE],
)
async def test_a_decision_that_carries_on_clears_the_fault_it_names(
    decision: RecoveryDecision,
) -> None:
    facade, runtime = _facade(_fault())

    await facade.recover("exec-1", "t1", decision, confirm=True)

    assert [f.device_id for f in runtime.cleared] == ["pf400_1"]
    assert runtime.recovered == [decision]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        RecoveryDecision.ABORT_ACTION,
        RecoveryDecision.ABORT_METHOD,
        RecoveryDecision.ABORT_THREAD,
    ],
)
async def test_an_abort_leaves_the_fault_standing(
    decision: RecoveryDecision,
) -> None:
    facade, runtime = _facade(_fault())

    await facade.recover("exec-1", "t1", decision, confirm=True)

    assert runtime.cleared == []
    assert runtime.recovered == [decision]


@pytest.mark.asyncio
async def test_a_pause_about_no_fault_clears_nothing() -> None:
    facade, runtime = _facade(None)

    await facade.recover("exec-1", "t1", RecoveryDecision.RETRY, confirm=True)

    assert runtime.cleared == []
    assert runtime.recovered == [RecoveryDecision.RETRY]


@pytest.mark.asyncio
async def test_the_fault_is_cleared_before_the_thread_resumes() -> None:
    """The resumed call is dispatched to the same device, and a fault still
    standing refuses it, which is the loop this fixes."""
    facade, runtime = _facade(_fault())
    order: List[str] = []

    async def note_clear(fault: DeviceFault) -> None:
        order.append("cleared")

    def note_recover(
        execution_id: str, thread_id: str, decision: RecoveryDecision,
    ) -> None:
        order.append("resumed")

    setattr(runtime, "clear_fault_if_current", note_clear)
    setattr(runtime, "recover_thread", note_recover)

    await facade.recover("exec-1", "t1", RecoveryDecision.RETRY, confirm=True)

    assert order == ["cleared", "resumed"]


class _PausedThread:
    def __init__(
        self, last_error: Exception | None = None, is_error_paused: bool = True,
    ) -> None:
        self.last_error = last_error
        self.is_error_paused = is_error_paused


class TestWhichFaultAPauseIsAbout:
    def test_the_command_that_faulted_the_device_carries_the_fault_it_left(
        self,
    ) -> None:
        """A failed move is the case that has no device call to read a name off,
        and it is the one the bench hit first."""
        error = CommandExecutionError("Stall or Collision Detected")
        error.device_fault = _fault(command="pick")

        named = _fault_named_by_pause(_PausedThread(last_error=error))

        assert named is not None
        assert named.command == "pick"

    def test_a_command_refused_by_a_standing_fault_names_that_one(self) -> None:
        fault = _fault(command="move_plate")

        named = _fault_named_by_pause(
            _PausedThread(last_error=DeviceFaultedError("refused", fault)),
        )

        assert named is fault

    def test_a_pause_about_no_device_names_no_fault(self) -> None:
        thread = _PausedThread(last_error=ValueError("variable unresolved"))

        assert _fault_named_by_pause(thread) is None

    def test_a_thread_that_is_no_longer_error_paused_names_nothing(self) -> None:
        """ABORT_THREAD keeps the last error on purpose and leaves the fault
        standing. A later RETRY on the aborted thread is refused, but it is
        refused AFTER the clear would have run, so reading the preserved error
        would drop a fault nobody looked at."""
        error = CommandExecutionError("Stall or Collision Detected")
        error.device_fault = _fault()

        thread = _PausedThread(last_error=error, is_error_paused=False)

        assert _fault_named_by_pause(thread) is None


@pytest.mark.asyncio
class TestOnlyTheFaultThePauseNamesIsCleared:
    """`clear_fault_if_current` runs for real here, against a controller this
    test owns rather than the module singleton it normally reads."""

    async def _clear(
        self, controller: DeviceController, fault: DeviceFault,
    ) -> None:
        with patch.object(system_runtime, "device_controller", controller):
            await SystemRuntime.clear_fault_if_current(MagicMock(), fault)

    async def test_a_different_fault_standing_on_the_device_is_left_alone(
        self,
    ) -> None:
        """One thread can pause on a device without faulting it while another
        thread's earlier failure is still standing there, unlooked-at."""
        controller = DeviceController()
        standing = _fault(device_id="flex_1", command="move_plate")
        controller._faults["flex_1"] = standing

        await self._clear(controller, _fault(device_id="flex_1", command="pick_up_tips"))

        assert controller.fault("flex_1") is standing

    async def test_the_named_fault_is_cleared(self) -> None:
        controller = DeviceController()
        standing = _fault(device_id="flex_1", command="move_plate")
        controller._faults["flex_1"] = standing

        await self._clear(controller, standing)

        assert controller.fault("flex_1") is None
