"""Unit tests for the ITrackedDevice dispatch helper and the build-time guard.

Verifies that ``resolve_operation_interpreter`` walks the MRO correctly to
find the first ITrackedDevice subclass that explicitly defines
``operation_interpreter``, and that the build-time validator rejects a
tracked device whose interpreter resolves to DefaultInterpreter.
"""

from typing import List

import pytest

from cheshire_drivers.labware_interfaces import ITipSpot, IWell
from cheshire_drivers.liquid_handler_models import (
    LabwareStateResponse,
    ReconcileHardwareStateResponse,
)
from cheshire_drivers.pipetting import MixParams, PipettingProfile

from orca.devices.device_interfaces import ILiquidHandler, ITrackedDevice
from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
from orca.resource_models.tracking_interpreter import (
    DefaultInterpreter,
    IOperationInterpreter,
    resolve_operation_interpreter,
)
from orca.sdk.build import (
    TrackedDeviceInterpreterError,
    _validate_tracked_device_interpreters,
)


_OK_RESPONSE = LabwareStateResponse(success=True)


class _BareDevice:
    """Untracked device implementing no ITrackedDevice interface."""

    def __init__(self, name: str = "bare") -> None:
        self.name = name


class _FakeRemoteLH(ILiquidHandler):
    """Subclasses ILiquidHandler without subclassing the concrete LiquidHandler.

    Models any over-the-wire LH that implements the interface but is NOT a
    subclass of the local concrete LiquidHandler. The production wire path
    pairs the concrete LiquidHandler with a wire-forwarding
    RemoteLiquidHandlerDriver at the driver layer, so this scenario is
    primarily for third-party / external implementers; the dispatcher must
    still find LiquidHandlerInterpreter via the ILiquidHandler classmethod.
    """

    def __init__(self, name: str = "remote_lh") -> None:
        self.name = name

    async def aspirate(
        self,
        wells: List[IWell],
        volumes: List[float],
        flow_rates: List[float] | None = None,
        offsets_z: List[float] | None = None,
        use_channels: List[int] | None = None,
        liquid_class: PipettingProfile | None = None,
        technique: PipettingProfile | None = None,
    ) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def dispense(
        self,
        wells: List[IWell],
        volumes: List[float],
        flow_rates: List[float] | None = None,
        offsets_z: List[float] | None = None,
        use_channels: List[int] | None = None,
        liquid_class: PipettingProfile | None = None,
        technique: PipettingProfile | None = None,
    ) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def pick_up_tips(self, tip_spots: List[ITipSpot]) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def drop_tips(self, tip_spots: List[ITipSpot]) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def discard_tips(self, use_channels: List[int] | None = None) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def reconcile_hardware_state(self) -> ReconcileHardwareStateResponse:
        return ReconcileHardwareStateResponse(checked=False)

    async def discard_stranded_tips(self) -> ReconcileHardwareStateResponse:
        return ReconcileHardwareStateResponse(checked=False)

    async def mix(
        self,
        wells: List[IWell],
        params: MixParams,
        use_channels: List[int] | None = None,
    ) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def aspirate96(
        self,
        labware: str,
        volume: float,
        flow_rate: float | None = None,
        liquid_height: float | None = None,
    ) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def dispense96(
        self,
        labware: str,
        volume: float,
        flow_rate: float | None = None,
        liquid_height: float | None = None,
    ) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def pick_up_tips96(self, tip_rack: str) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def drop_tips96(self, tip_rack: str | None = None) -> LabwareStateResponse:
        return _OK_RESPONSE

    async def return_tips96(self) -> LabwareStateResponse:
        return _OK_RESPONSE


class _IBrokenTracked(ITrackedDevice):
    """A tracked interface whose classmethod returns DefaultInterpreter.

    This is the exact failure mode the build-time guard exists to catch:
    an interface declares itself tracked but routes to the catch-all
    DefaultInterpreter, silently dropping typed records.
    """

    @classmethod
    def operation_interpreter(cls) -> IOperationInterpreter:
        return DefaultInterpreter()


class _BrokenTrackedDevice(_IBrokenTracked):
    def __init__(self, name: str = "broken") -> None:
        self.name = name


class TestResolveOperationInterpreter:

    def test_remote_lh_resolves_to_liquid_handler_interpreter(self) -> None:
        device = _FakeRemoteLH("remote")
        interpreter = resolve_operation_interpreter(device)
        assert isinstance(interpreter, LiquidHandlerInterpreter)

    def test_bare_device_resolves_to_default_interpreter(self) -> None:
        device = _BareDevice("bare")
        interpreter = resolve_operation_interpreter(device)
        assert isinstance(interpreter, DefaultInterpreter)

    def test_concrete_subclass_inherits_interpreter_without_override(self) -> None:
        """Concrete LH device subclassing ILiquidHandler picks up the interpreter
        through MRO without redeclaring operation_interpreter."""

        class _ConcreteLH(_FakeRemoteLH):
            pass

        interpreter = resolve_operation_interpreter(_ConcreteLH("concrete"))
        assert isinstance(interpreter, LiquidHandlerInterpreter)


class TestBuildTimeGuard:

    def test_remote_lh_passes_validation(self) -> None:
        _validate_tracked_device_interpreters([_FakeRemoteLH("lh_1")])

    def test_untracked_device_passes_validation(self) -> None:
        device = _BareDevice("bare")
        # The guard only inspects ITrackedDevice resources; an untracked
        # device is skipped (not validated against an interpreter at all).
        assert not isinstance(device, ITrackedDevice)
        assert resolve_operation_interpreter(device).__class__ is DefaultInterpreter
        assert _validate_tracked_device_interpreters([device]) is None

    def test_tracked_device_with_default_interpreter_raises(self) -> None:
        with pytest.raises(TrackedDeviceInterpreterError) as excinfo:
            _validate_tracked_device_interpreters([_BrokenTrackedDevice("broken")])
        assert "broken" in str(excinfo.value)
        assert "_IBrokenTracked" in str(excinfo.value)

    def test_validation_walks_every_resource(self) -> None:
        with pytest.raises(TrackedDeviceInterpreterError):
            _validate_tracked_device_interpreters([
                _FakeRemoteLH("good"),
                _BareDevice("bare"),
                _BrokenTrackedDevice("broken"),
            ])
