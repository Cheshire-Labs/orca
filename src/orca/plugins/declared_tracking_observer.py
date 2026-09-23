"""Observer that synthesizes typed OperationRecords from a DeclaredTracking.

Used when a device runs a closed protocol (no PLR observer is available)
and the author annotates the action with ``declares=DeclaredTracking(...)``.
Emits the same shape of OperationRecord that PLR observation would emit,
so downstream projections (well_volume, tips_present, op_count) work
uniformly across declared and observed modes.

Conventions:
- DeclaredVolumeTransfer.volume_ul is per-channel. Source_wells and
  target_wells may have different cardinalities (1:N fan-out, N:1 pool);
  the aspirate op matches source_wells length, the dispense op matches
  target_wells length, and volumes are length-matched to positions.
- Aspirate+dispense pairs share a freshly-minted group_id (uuid4) so a
  consumer can reconstruct the transfer as a unit.
- Black-box case (source_wells=None, target_wells=None) emits ops with
  positions=[]; well_volumes will not decrement on that labware
  (documented drift in ledger_projections.well_volumes).
"""
import time
import uuid

from orca.events.execution_context import MethodExecutionContext
from orca.resource_models.labware import LabwareInstance
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    DispenseDetails,
    OperationRecord,
    TipPickUpDetails,
    TrackingRecord,
    TrackingSource,
    WellUsageDetails,
)
from orca.state.records import DeclaredTracking


def _resolve(
    template_name: str,
    template_to_instance: dict[str, LabwareInstance] | None,
) -> tuple[str, list[str]]:
    """Resolve a template name (as written in declares) to (instance name, ids).

    Declares keys are template-scoped because that is what the user writes
    at method-authoring time. The ops_history bucketing is instance-scoped
    so multiple physical instances of the same template don't collide.
    When no mapping is provided or the template isn't present (e.g. a
    transfer references a labware not assigned to this action), fall back
    to the template name so the op is still recorded rather than dropped.

    The second element is the canonical labware UUID list (one id, or empty
    when no instance resolved) that the journey / history lookups accept.
    """
    if template_to_instance is None:
        return template_name, []
    instance = template_to_instance.get(template_name)
    if instance is None:
        return template_name, []
    return instance.name, [instance.id]


class DeclaredTrackingObserver:
    def process_operations(
        self,
        operations: list[OperationRecord],
        execution_context: MethodExecutionContext,
        action_id: str,
        thread_id: str,
        declares: DeclaredTracking | None = None,
        template_to_instance: dict[str, LabwareInstance] | None = None,
    ) -> TrackingRecord | None:
        now = time.time()

        # Action has no DeclaredTracking but the interpreter still populated
        # operations (e.g. LiquidHandlerInterpreter emits AspirateDetails for
        # every aspirate call, plus DRIVER_OBSERVED records from interpret_
        # driver_state). Without this passthrough the records were dropped
        # silently: hundreds of aspirate/dispense calls but ops_history
        # ends up empty.
        if declares is None:
            if not operations:
                return None
            return TrackingRecord(
                action_id=action_id,
                thread_id=thread_id,
                method_id=execution_context.method_id,
                source=TrackingSource.OBSERVED,
                timestamp=now,
                operations=list(operations),
                execution_id=execution_context.execution_id,
            )

        synthesized: list[OperationRecord] = list(operations)

        if declares.volume_transferred:
            for vt in declares.volume_transferred:
                group_id = str(uuid.uuid4())
                src_positions = vt.source_wells or []
                tgt_positions = vt.target_wells or []
                src_inst, src_ids = _resolve(vt.source, template_to_instance)
                tgt_inst, tgt_ids = _resolve(vt.target, template_to_instance)
                synthesized.append(
                    OperationRecord(
                        operation=DeviceOperation.ASPIRATE,
                        device_name="",
                        affected_labware=[src_inst],
                        affected_labware_ids=src_ids,
                        action_id=action_id,
                        thread_id=thread_id,
                        details=AspirateDetails(
                            labware=src_inst,
                            positions=list(src_positions),
                            volumes=[vt.volume_ul] * len(src_positions),
                        ),
                        timestamp=now,
                        group_id=group_id,
                    )
                )
                synthesized.append(
                    OperationRecord(
                        operation=DeviceOperation.DISPENSE,
                        device_name="",
                        affected_labware=[tgt_inst],
                        affected_labware_ids=tgt_ids,
                        action_id=action_id,
                        thread_id=thread_id,
                        details=DispenseDetails(
                            labware=tgt_inst,
                            positions=list(tgt_positions),
                            volumes=[vt.volume_ul] * len(tgt_positions),
                        ),
                        timestamp=now,
                        group_id=group_id,
                    )
                )

        if declares.tips_used:
            for rack_template, positions in declares.tips_used.items():
                rack_inst, rack_ids = _resolve(rack_template, template_to_instance)
                synthesized.append(
                    OperationRecord(
                        operation=DeviceOperation.PICK_UP_TIPS,
                        device_name="",
                        affected_labware=[rack_inst],
                        affected_labware_ids=rack_ids,
                        action_id=action_id,
                        thread_id=thread_id,
                        details=TipPickUpDetails(tip_rack=rack_inst, positions=list(positions)),
                        timestamp=now,
                    )
                )

        if declares.wells_used:
            for lw_template, positions in declares.wells_used.items():
                lw_inst, lw_ids = _resolve(lw_template, template_to_instance)
                synthesized.append(
                    OperationRecord(
                        operation=DeviceOperation.WELL_USAGE,
                        device_name="",
                        affected_labware=[lw_inst],
                        affected_labware_ids=lw_ids,
                        action_id=action_id,
                        thread_id=thread_id,
                        details=WellUsageDetails(labware=lw_inst, positions=list(positions)),
                        timestamp=now,
                    )
                )

        if declares.operations:
            for op_type, details in declares.operations:
                # affected_labware extracted from details.labware when present.
                affected: list[str] = []
                affected_ids: list[str] = []
                if hasattr(details, "labware"):
                    name, ids = _resolve(getattr(details, "labware"), template_to_instance)
                    affected.append(name)
                    affected_ids.extend(ids)
                elif hasattr(details, "tip_rack"):
                    tip_rack = getattr(details, "tip_rack")
                    if isinstance(tip_rack, str):
                        name, ids = _resolve(tip_rack, template_to_instance)
                        affected.append(name)
                        affected_ids.extend(ids)
                synthesized.append(
                    OperationRecord(
                        operation=op_type,
                        device_name="",
                        affected_labware=affected,
                        affected_labware_ids=affected_ids,
                        action_id=action_id,
                        thread_id=thread_id,
                        details=details,
                        timestamp=now,
                    )
                )

        return TrackingRecord(
            action_id=action_id,
            thread_id=thread_id,
            method_id=execution_context.method_id,
            source=TrackingSource.DECLARED,
            timestamp=now,
            operations=synthesized,
            execution_id=execution_context.execution_id,
        )
