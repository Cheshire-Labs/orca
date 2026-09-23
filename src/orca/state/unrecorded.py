"""Operations an action has performed that are not in the record yet.

A device op produces its record the moment the call returns, but the records
ride on the action and are folded into the ledger only once the action
finishes. An action that failed halfway is therefore sitting on a real
pick-up and a real aspirate that no read can see.

Being behind is unavoidable and fine. Answering ``known`` while behind is
neither: the tip pre-flight reads the ledger, so a confident wrong answer
defeats a safety check rather than merely misinforming a person. A read
consults this to find out whether it is behind, and downgrades itself to
``stale`` when it is.

Entries are held weakly. Every path that folds an action's operations drops
its entry, and an action dropped some other way (an aborted thread) takes its
entry with it rather than leaving every later read stale forever.
"""

from typing import Iterator, Protocol
from weakref import WeakValueDictionary

from orca.state.records import OperationRecord


class PerformsOperations(Protocol):
    """An action that accumulates operation records until something folds them."""

    @property
    def pending_operations(self) -> list[OperationRecord]: ...


class UnrecordedOperations:
    """Which actions are holding operations the ledger has not been told about."""

    def __init__(self) -> None:
        self._actions: WeakValueDictionary[str, PerformsOperations] = (
            WeakValueDictionary()
        )

    def watch(self, action_id: str, action: PerformsOperations) -> None:
        """Follow this action's operations until they are folded or it is gone.

        Re-watching one action replaces the entry: a retry runs against a fresh
        log and the old one has already been folded into the retry's record.
        """
        self._actions[action_id] = action

    def forget(self, action_id: str) -> None:
        """This action's operations are in the record now."""
        self._actions.pop(action_id, None)

    def touches_device(self, device_name: str) -> bool:
        return any(op.device_name == device_name for op in self._pending())

    def touches_labware(self, labware_name: str) -> bool:
        return any(labware_name in op.affected_labware for op in self._pending())

    def _pending(self) -> Iterator[OperationRecord]:
        for action in list(self._actions.values()):
            yield from action.pending_operations
