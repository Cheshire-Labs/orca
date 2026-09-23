"""Tests for LabwareGroup validation logic."""

from uuid import uuid4

import pytest

from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember


def test_group_rejects_duplicate_thread_templates() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        LabwareGroup(
            id=str(uuid4()),
            members=(
                LabwareGroupMember(thread_template_name="sample_journey"),
                LabwareGroupMember(thread_template_name="sample_journey"),
            ),
        )


def test_member_for_thread_returns_matching_member_or_none() -> None:
    sample = LabwareGroupMember(thread_template_name="sample_journey")
    primer = LabwareGroupMember(thread_template_name="primer_journey")
    g = LabwareGroup(id=str(uuid4()), members=(sample, primer))

    assert g.member_for_thread("sample_journey") is sample
    assert g.member_for_thread("missing") is None
