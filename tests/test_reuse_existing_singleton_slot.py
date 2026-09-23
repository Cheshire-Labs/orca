"""A start_reuse_existing (deck-resident) thread is a physical singleton.

A reagent trough bound via REUSE_EXISTING is one physical instance on a fixed
carrier, drawn from by every group and submission in a batch. Its receiver slot
must therefore be keyed by labware name alone: a per-group or per-submission key
gives each group its OWN slot, and each group's auto-spawn then independently
mints and start()s the SAME singleton resident thread. That double-start runs the
thread's method-lane generator from two tasks at once and crashes with
``anext(): asynchronous generator is already running`` (the multi-plate SMC
shared-reagent deadlock).
"""

from unittest.mock import Mock

from orca.resource_models.labware import TroughTemplate
from orca.runtime.group_aware_labware_registry import GroupAwareLabwareRegistry
from orca.runtime.group_execution_context import GroupExecutionContext


def _resident_template(name: str) -> Mock:
    labware_template = TroughTemplate(name, labware_type="_test_placeholder")
    thread_template = Mock()
    thread_template.labware_template = labware_template
    thread_template.start_reuse_existing = True
    return thread_template


def test_resident_collapses_group_component() -> None:
    registry = GroupAwareLabwareRegistry()
    template = _resident_template("bead_reservoir")
    ctx_a = GroupExecutionContext(group_id="grp-0", submission_id="sub-1")
    ctx_b = GroupExecutionContext(group_id="grp-1", submission_id="sub-1")

    key_a = registry.slot_key_for(template, ctx_a)
    key_b = registry.slot_key_for(template, ctx_b)

    assert key_a == key_b, (
        "two groups drawing from the one physical resident trough must share a "
        "single receiver slot; a per-group key double-starts the singleton thread"
    )


def test_resident_collapses_submission_component() -> None:
    registry = GroupAwareLabwareRegistry()
    template = _resident_template("bead_reservoir")
    ctx_a = GroupExecutionContext(group_id="grp-0", submission_id="sub-1")
    ctx_b = GroupExecutionContext(group_id="grp-0", submission_id="sub-2")

    key_a = registry.slot_key_for(template, ctx_a)
    key_b = registry.slot_key_for(template, ctx_b)

    assert key_a == key_b, (
        "a later JOIN_EXISTING submission draws from the same physical resident "
        "trough, so it must address the same receiver slot"
    )


def test_resident_slot_key_matches_bare_labware_name() -> None:
    registry = GroupAwareLabwareRegistry()
    template = _resident_template("bead_reservoir")
    ctx = GroupExecutionContext(group_id="grp-0", submission_id="sub-1")

    assert registry.slot_key_for(template, ctx) == "bead_reservoir"
