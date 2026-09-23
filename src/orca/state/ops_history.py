"""Per-execution write/read facade over the OpsHistoryStore.

Single source of truth for what happened on each labware. Both write paths
(action records and initial-state seeds) and read paths (records,
all_operations, ops_for) are async; the underlying store is async, callers
are async, no sync-from-async hack.

Lives on ISystem so the system functions standalone -- ledger projections
read through ``await ops_history.ops_for(name)`` without needing to know
which execution the labware belongs to.
"""
import time
import uuid
from typing import List

from typing_extensions import Self

from orca.state.records import (
    DeviceOperation,
    InitialStateDetails,
    ObservationGapCause,
    ObservationGapDetails,
    OperationRecord,
    SetTipStateDetails,
    HeadObservationGapDetails,
    MountedTipsAssertedDetails,
    MountedTipsConfirmedDetails,
    SetVolumeDetails,
    TrackingRecord,
    TrackingSource,
)
from orca.state.jsonl_store import JsonlOpsHistoryStore
from orca.state.ops_store import (
    IOpsHistoryStore,
    SYSTEM_ID,
)
from orca.state.unrecorded import UnrecordedOperations


class OpsHistory:
    """Per-execution writer + reader over an IOpsHistoryStore.

    The store is system-owned and shared across executions. Each
    ExecutingWorkflow gets its own OpsHistory bound to its execution_id;
    initial-state seeds for labware spawned outside any execution use the
    ``SYSTEM_ID`` sentinel.

    All access (read and write) is async because the store may do real
    I/O on the hosted side. UIs and remote consumers go through
    ``IOpsHistoryFacade`` for the same data with cross-execution search.
    """

    def __init__(
        self,
        store: IOpsHistoryStore | None = None,
        execution_id: str = SYSTEM_ID,
        unrecorded: UnrecordedOperations | None = None,
    ) -> None:
        self._store: IOpsHistoryStore = store or JsonlOpsHistoryStore.ephemeral()
        self._execution_id = execution_id
        self._unrecorded = unrecorded if unrecorded is not None else UnrecordedOperations()

    @property
    def store(self) -> IOpsHistoryStore:
        return self._store

    @property
    def unrecorded(self) -> UnrecordedOperations:
        """Actions holding operations that have not reached the store yet.

        Shared with every view of this history, so a read taken through one
        execution's view still learns about an action running under another.
        """
        return self._unrecorded

    @property
    def execution_id(self) -> str:
        return self._execution_id

    def bind_store(self, store: IOpsHistoryStore) -> None:
        """Replace the backing store. Used by a hosted deployment at lifecycle
        build to swap the source-available default for its own indexed impl after
        deployment_package.build() returned its SystemBuild. Must be called
        before any execution writes (no record migration is performed).
        """
        self._store = store

    def for_execution(self, execution_id: str) -> Self:
        """Return a new OpsHistory view over the same store, bound to ``execution_id``."""
        return type(self)(
            store=self._store, execution_id=execution_id,
            unrecorded=self._unrecorded,
        )

    async def append_record(self, record: TrackingRecord) -> None:
        """Append a record emitted by an observer (declared or observed mode)."""
        await self._store.append(self._execution_id, record)

    async def append_initial_state(
        self, labware_name: str, details: InitialStateDetails,
        labware_id: str | None = None,
    ) -> None:
        """Append an INITIAL_STATE op for a newly-created labware instance.

        Synthesizes a minimal TrackingRecord so the initial seed flows
        through the same storage as action-originated records.

        Labware names must be unique across instances within a single
        execution bucket: ops_history buckets by name, so two instances
        sharing a name would scramble each other's tracked state. If an
        INITIAL_STATE already exists for this name in this bucket, raise
        to surface the naming collision at seed time.

        Pass ``labware_id`` whenever the caller holds the instance: an
        id-less record cannot be told apart from a same-named successor.
        """
        for committed in await self._store.list(self._execution_id):
            for op in committed.operations:
                # A driver's own snapshot wears INITIAL_STATE too. It is a
                # witness to a labware already seeded, never a second seed, so
                # counting it here would refuse the real one.
                if op.source is TrackingSource.DRIVER_OBSERVED:
                    continue
                if op.operation == DeviceOperation.INITIAL_STATE and labware_name in op.affected_labware:
                    raise ValueError(
                        f"Labware name '{labware_name}' already has an INITIAL_STATE op. "
                        f"Two labware instances share this name; their tracking state would "
                        f"collide. Labware instance names must be unique."
                    )
        action_id = f"__init__:{labware_name}"
        now = time.time()
        record = TrackingRecord(
            execution_id=self._execution_id,
            action_id=action_id,
            thread_id=SYSTEM_ID,
            method_id=None,
            source=TrackingSource.DECLARED,
            timestamp=now,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.INITIAL_STATE,
                    device_name="__template__",
                    affected_labware=[labware_name],
                    affected_labware_ids=[labware_id] if labware_id is not None else [],
                    action_id=action_id,
                    thread_id=SYSTEM_ID,
                    details=details,
                    timestamp=now,
                )
            ],
        )
        await self._store.append(self._execution_id, record)

    async def append_set_volume(
        self, labware_name: str, well_volumes: dict[str, float],
        labware_id: str | None = None,
    ) -> None:
        """Append an operator SET_VOLUME op (absolute per-well overwrite).

        Unlike INITIAL_STATE there is no first-record uniqueness guard: an
        operator may set volumes repeatedly, and the fold applies the latest
        one. ``source=OPERATOR`` lets audit / can_continue see a hand-set
        value distinct from observed or declared state.
        """
        action_id = f"__set_volume__:{labware_name}:{uuid.uuid4().hex[:8]}"
        now = time.time()
        record = TrackingRecord(
            execution_id=self._execution_id,
            action_id=action_id,
            thread_id=SYSTEM_ID,
            method_id=None,
            source=TrackingSource.OPERATOR,
            timestamp=now,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.SET_VOLUME,
                    device_name="__operator__",
                    affected_labware=[labware_name],
                    affected_labware_ids=[labware_id] if labware_id is not None else [],
                    action_id=action_id,
                    thread_id=SYSTEM_ID,
                    details=SetVolumeDetails(labware=labware_name, well_volumes=dict(well_volumes)),
                    timestamp=now,
                    source=TrackingSource.OPERATOR,
                )
            ],
        )
        await self._store.append(self._execution_id, record)

    async def append_set_mounted_tips(
        self, device_name: str, by_channel: dict[int, tuple[str, str]],
    ) -> None:
        """Append an operator statement of what a head is carrying.

        Absolute: an empty map says the head is carrying nothing, which is a
        statement rather than silence.
        """
        action_id = f"__set_mounted_tips__:{device_name}:{uuid.uuid4().hex[:8]}"
        now = time.time()
        record = TrackingRecord(
            execution_id=self._execution_id,
            action_id=action_id,
            thread_id=SYSTEM_ID,
            method_id=None,
            source=TrackingSource.OPERATOR,
            timestamp=now,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.SET_MOUNTED_TIPS,
                    device_name=device_name,
                    affected_labware=[],
                    affected_labware_ids=[],
                    action_id=action_id,
                    thread_id=SYSTEM_ID,
                    details=MountedTipsAssertedDetails(by_channel=dict(by_channel)),
                    timestamp=now,
                    source=TrackingSource.OPERATOR,
                )
            ],
        )
        await self._store.append(self._execution_id, record)

    async def append_confirm_mounted_tips(self, device_name: str) -> None:
        """Append an operator's agreement with what the record already says.

        Names no tips. A confirm settles who has looked; restating the head
        would turn a channel number the record only guessed into one an
        operator claimed to have observed.
        """
        action_id = f"__confirm_mounted_tips__:{device_name}:{uuid.uuid4().hex[:8]}"
        now = time.time()
        record = TrackingRecord(
            execution_id=self._execution_id,
            action_id=action_id,
            thread_id=SYSTEM_ID,
            method_id=None,
            source=TrackingSource.OPERATOR,
            timestamp=now,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.CONFIRM_MOUNTED_TIPS,
                    device_name=device_name,
                    affected_labware=[],
                    affected_labware_ids=[],
                    action_id=action_id,
                    thread_id=SYSTEM_ID,
                    details=MountedTipsConfirmedDetails(device_name=device_name),
                    timestamp=now,
                    source=TrackingSource.OPERATOR,
                )
            ],
        )
        await self._store.append(self._execution_id, record)

    async def append_set_tip_state(
        self, labware_name: str, tip_positions_present: list[str],
        labware_id: str | None = None,
    ) -> None:
        """Append an operator SET_TIP_STATE op (absolute tip-layout overwrite).

        Like SET_VOLUME: no uniqueness guard, the fold applies the latest one,
        and ``source=OPERATOR`` keeps hand-asserted state distinct from
        observed or declared state.
        """
        action_id = f"__set_tip_state__:{labware_name}:{uuid.uuid4().hex[:8]}"
        now = time.time()
        record = TrackingRecord(
            execution_id=self._execution_id,
            action_id=action_id,
            thread_id=SYSTEM_ID,
            method_id=None,
            source=TrackingSource.OPERATOR,
            timestamp=now,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.SET_TIP_STATE,
                    device_name="__operator__",
                    affected_labware=[labware_name],
                    affected_labware_ids=[labware_id] if labware_id is not None else [],
                    action_id=action_id,
                    thread_id=SYSTEM_ID,
                    details=SetTipStateDetails(
                        labware=labware_name,
                        tip_positions_present=list(tip_positions_present),
                    ),
                    timestamp=now,
                    source=TrackingSource.OPERATOR,
                )
            ],
        )
        await self._store.append(self._execution_id, record)

    async def append_observation_gap(
        self, labware_name: str, cause: ObservationGapCause,
        labware_id: str | None = None,
    ) -> None:
        """Append the fact that nobody was watching this labware for a while.

        Moves no volume and no tip. Its only effect on a projection is that
        anything known before it stops counting as current, so the read asks to
        be looked at rather than answering as known.
        """
        action_id = f"__observation_gap__:{labware_name}:{uuid.uuid4().hex[:8]}"
        now = time.time()
        record = TrackingRecord(
            execution_id=self._execution_id,
            action_id=action_id,
            thread_id=SYSTEM_ID,
            method_id=None,
            source=TrackingSource.OBSERVED,
            timestamp=now,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.OBSERVATION_GAP,
                    device_name="__system__",
                    affected_labware=[labware_name],
                    affected_labware_ids=[labware_id] if labware_id is not None else [],
                    action_id=action_id,
                    thread_id=SYSTEM_ID,
                    details=ObservationGapDetails(labware=labware_name, cause=cause),
                    timestamp=now,
                )
            ],
        )
        await self._store.append(self._execution_id, record)

    async def append_head_observation_gap(
        self, device_name: str, cause: ObservationGapCause,
    ) -> None:
        """Nobody was watching this head for a while.

        Carried on the device rather than a labware, because what it expires is
        an attestation about the channels, and the tips on them belong to no
        one rack.
        """
        action_id = f"__observation_gap__:{device_name}:{uuid.uuid4().hex[:8]}"
        now = time.time()
        record = TrackingRecord(
            execution_id=self._execution_id,
            action_id=action_id,
            thread_id=SYSTEM_ID,
            method_id=None,
            source=TrackingSource.OBSERVED,
            timestamp=now,
            operations=[
                OperationRecord(
                    operation=DeviceOperation.OBSERVATION_GAP,
                    device_name=device_name,
                    affected_labware=[],
                    affected_labware_ids=[],
                    action_id=action_id,
                    thread_id=SYSTEM_ID,
                    details=HeadObservationGapDetails(
                        device_name=device_name, cause=cause,
                    ),
                    timestamp=now,
                )
            ],
        )
        await self._store.append(self._execution_id, record)

    async def records(self) -> List[TrackingRecord]:
        """Materialize the bound execution's bucket as a list of records."""
        return await self._store.list(self._execution_id)

    async def all_operations(self) -> List[OperationRecord]:
        return [op for rec in await self.records() for op in rec.operations]

    async def ops_for(self, labware_name: str) -> List[OperationRecord]:
        return [op for op in await self.all_operations() if labware_name in op.affected_labware]
