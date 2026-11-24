from typing import Protocol, TypeVar

from cheshire_drivers.liquid_handler_models import LabwareStateResponse
from pydantic import BaseModel

from orca.state.command_operations import action_context, operations_for_command
from orca.state.records import OperationRecord

# The interpreter surface is generic over the device call's argument values;
# the dispatcher narrows the call's return to LabwareStateResponse | None.
V = TypeVar("V")


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
        channels. A tip pick-up, drop or discard is read the same way: one
        record per channel, and a failed channel's record moves no tip.
        Returning a non-empty list signals the action body to ERROR the action
        after persisting the records.
        """
        ...

    def request_of(
        self, command: str, args: tuple[V, ...], kwargs: dict[str, V],
    ) -> BaseModel | None:
        """The typed request a raw action call stands for, or None when the interpreter has none for it.

        Read before dispatch, so a run's tip pick passes the same tip check as an operator's.
        """
        ...

    def claimed_reagent_volumes(
        self, command: str, args: tuple[V, ...], kwargs: dict[str, V],
    ) -> dict[str, float]:
        """Trough name -> total volume, for a call about to aspirate from a trough."""
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
        return operations_for_command(command, repr(args), action_context(
            device_name, affected_labware, affected_labware_ids, action_id, thread_id,
        ))

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

    def request_of(
        self, command: str, args: tuple[V, ...], kwargs: dict[str, V],
    ) -> BaseModel | None:
        return None

    def claimed_reagent_volumes(
        self, command: str, args: tuple[V, ...], kwargs: dict[str, V],
    ) -> dict[str, float]:
        return {}
