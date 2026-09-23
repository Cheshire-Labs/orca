import time
from typing import Protocol, TypeVar

from cheshire_drivers.liquid_handler_models import LabwareStateResponse

from orca.state.records import (
    DeviceOperation,
    GenericOperationDetails,
    OperationRecord,
)

# The interpreter surface is generic over the device call's argument values;
# the dispatcher narrows the call's return to LabwareStateResponse | None.
V = TypeVar("V")


def resolve_operation_interpreter(device: object) -> "IOperationInterpreter":
    """Pick the IOperationInterpreter for ``device`` by walking its MRO.

    Returns the first ``ITrackedDevice`` subclass in ``type(device).__mro__``
    (excluding ``ITrackedDevice`` itself) that explicitly defines its own
    ``operation_interpreter`` classmethod. Falls through to ``DefaultInterpreter``
    when no tracked interface is found, which is correct for untracked devices
    (DefaultInterpreter emits a generic record per call) but would silently
    drop typed records for tracked ones; the build-time guard in
    ``orca.sdk.build`` rejects that case loud.
    """
    from orca.devices.device_interfaces import ITrackedDevice
    for cls in type(device).__mro__:
        if not isinstance(cls, type) or cls is ITrackedDevice:
            continue
        if not issubclass(cls, ITrackedDevice):
            continue
        own = cls.__dict__.get("operation_interpreter")
        if own is None:
            continue
        return cls.operation_interpreter()
    return DefaultInterpreter()


class IOperationInterpreter(Protocol):
    def interpret(
        self,
        command: str,
        args: tuple[V, ...],
        kwargs: dict[str, V],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """Every record this call produced, in the order the channels acted.

        A list rather than one record: a head operation reaching across two
        labware is two facts about two labware, and collapsing them is how a
        tip rack got debited for another rack's tips.
        """
        ...

    def interpret_driver_state(
        self,
        command: str,
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """Emit DRIVER_OBSERVED records reflecting the driver's reported state.

        Called after ``interpret``. Returns an empty list when the result has
        no driver-reported state (e.g., result is None or carries no labware
        state). When non-empty, each record has ``source=TrackingSource.DRIVER_OBSERVED``
        and represents the driver's view of well volumes / tip presence at the
        moment the call returned.
        """
        ...

    def interpret_per_channel_outcomes(
        self,
        command: str,
        args: tuple[V, ...],
        kwargs: dict[str, V],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """Emit per-well records when the call had a per-channel partial failure.

        Returns an empty list when the result has no per-channel error info,
        which is true on full success (the interpreter then emits the standard
        consolidated record via ``interpret``) and on opaque exceptions. When
        non-empty, every well in the request has its own record carrying the
        per-well certainty: ``confirmed_transferred`` for successful channels,
        ``definitely_not_transferred`` (volumes=[0] + error_code) for failed
        channels. Returning a non-empty list signals the action body to ERROR
        the action after persisting the records.
        """
        ...

    def claimed_tip_positions(
        self, command: str, args: tuple[V, ...], kwargs: dict[str, V],
    ) -> dict[str, list[str]]:
        """Rack name -> requested tip positions, for a call about to pick tips.

        Read before dispatch, against the raw call the action body made, so
        the requested positions can be checked against the ledger before the
        device call runs rather than after the instrument discovers them
        empty. Empty for every verb that is not a tip pick; the default
        interpreter has no tip concept at all.
        """
        ...


class DefaultInterpreter:
    def interpret(
        self,
        command: str,
        args: tuple[V, ...],
        kwargs: dict[str, V],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        try:
            op = DeviceOperation(command)
        except ValueError:
            return []
        return [OperationRecord(
            operation=op,
            device_name=device_name,
            affected_labware=affected_labware,
            affected_labware_ids=affected_labware_ids,
            action_id=action_id,
            thread_id=thread_id,
            details=GenericOperationDetails(command=command, args_repr=repr(args)),
            timestamp=time.time(),
        )]

    def interpret_driver_state(
        self,
        command: str,
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """Default interpreter has no driver-state knowledge; returns empty."""
        return []

    def interpret_per_channel_outcomes(
        self,
        command: str,
        args: tuple[V, ...],
        kwargs: dict[str, V],
        result: LabwareStateResponse | None,
        device_name: str,
        affected_labware: list[str],
        affected_labware_ids: list[str],
        action_id: str,
        thread_id: str,
    ) -> list[OperationRecord]:
        """Default interpreter has no per-channel concept; returns empty."""
        return []

    def claimed_tip_positions(
        self, command: str, args: tuple[V, ...], kwargs: dict[str, V],
    ) -> dict[str, list[str]]:
        """Default interpreter has no tip concept; returns empty."""
        return {}
