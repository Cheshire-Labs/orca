"""T6d: SHARED_ACROSS_GROUPS collapses the group_id component of the slot key.

Two groups in one submission whose contributors target a SHARED_ACROSS_GROUPS
template must resolve to the same LabwareSlot (one receiver across groups).
A PER_GROUP template keeps distinct per-group slots.
"""

from unittest.mock import Mock

from orca.resource_models.labware import PlateTemplate
from orca.resource_models.sharing import GroupSharing
from orca.runtime.group_aware_labware_registry import GroupAwareLabwareRegistry
from orca.runtime.group_execution_context import GroupExecutionContext


def _make_template(name: str, group_sharing: GroupSharing) -> Mock:
    labware_template = PlateTemplate(name, labware_type="_test_placeholder", group_sharing=group_sharing)
    thread_template = Mock()
    thread_template.labware_template = labware_template
    thread_template.start_reuse_existing = False
    return thread_template


def test_shared_across_groups_collapses_group_component() -> None:
    registry = GroupAwareLabwareRegistry()
    template = _make_template("reservoir", GroupSharing.SHARED_ACROSS_GROUPS)
    ctx_a = GroupExecutionContext(group_id="g-A", submission_id="sub-1")
    ctx_b = GroupExecutionContext(group_id="g-B", submission_id="sub-1")

    key_a = registry.slot_key_for(template, ctx_a)
    key_b = registry.slot_key_for(template, ctx_b)

    assert key_a == key_b == "reservoir:*:sub-1"


def test_per_group_keeps_distinct_slots() -> None:
    registry = GroupAwareLabwareRegistry()
    template = _make_template("sample_plate", GroupSharing.PER_GROUP)
    ctx_a = GroupExecutionContext(group_id="g-A", submission_id="sub-1")
    ctx_b = GroupExecutionContext(group_id="g-B", submission_id="sub-1")

    key_a = registry.slot_key_for(template, ctx_a)
    key_b = registry.slot_key_for(template, ctx_b)

    assert key_a != key_b
    assert key_a == "sample_plate:g-A:sub-1"
    assert key_b == "sample_plate:g-B:sub-1"
