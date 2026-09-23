"""ThreadSnapshot exposes labware_template_name (Bug KKK support).

Operator surfaces (a hosted REST insert-method, MCP thread_insert_method)
need the thread's labware template name at insert time so they can
pre-validate that the inserted method has at least one action whose
inputs accept the thread's labware. Pre-fix the field didn't exist
on the snapshot, so the validation could not be reached without
inventing a parallel facade method. Adding it to ThreadSnapshot keeps
the introspection on the typed read surface every operator already
queries.
"""

from typing import Callable
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance, LabwareTemplate, PlateTemplate
from orca.runtime.status_builders import _build_thread_snapshot
from orca.workflow_models.status_enums import LabwareThreadStatus


def _make_thread(*, labware_template: LabwareTemplate | None) -> MagicMock:
    thread = MagicMock()
    thread.id = "t1"
    thread.name = "plate_1-abcd"
    thread.status = LabwareThreadStatus.EXECUTING_ACTION
    thread.current_location.name = "stacker_3"
    thread.assigned_method = None
    thread.completed_methods = []
    thread.last_error = None
    thread.labware_template = labware_template
    return thread


def _real_plate_template() -> LabwareTemplate:
    return PlateTemplate(name="plate_1", labware_type="Cor_96_wellplate_360ul_Fb")


@pytest.mark.parametrize(
    ("labware_template_factory", "expected_name"),
    [
        (_real_plate_template, "plate_1"),
        (lambda: None, None),
    ],
)
def test_snapshot_labware_template_name(
    labware_template_factory: Callable[[], LabwareTemplate | None],
    expected_name: str | None,
) -> None:
    labware_template = labware_template_factory()
    snap = _build_thread_snapshot(_make_thread(labware_template=labware_template))

    assert snap.labware_template_name == expected_name


def test_snapshot_names_the_carried_instance() -> None:
    """The template alone cannot be discharged. An operator releasing an
    AWAITING_MANUAL_REMOVE park needs the instance the thread is carrying,
    and the poll that finds the park reads this snapshot.

    The stub mirrors the real object graph, where a thread's id and name ARE
    its labware's. Handing the snapshot a thread
    whose id differs from its labware's would pin a state that cannot occur
    and would hide the two fields reading the same value.
    """
    labware = LabwareInstance("plate_1", "Cor_96_wellplate_360ul_Fb", name="plate_1-abcd")
    thread = _make_thread(labware_template=_real_plate_template())
    thread.labware = labware
    thread.id = labware.id
    thread.name = labware.name

    snap = _build_thread_snapshot(thread)

    assert snap.labware_id == labware.id
    assert snap.labware_name == "plate_1-abcd"
    # Named separately on purpose, but equal by construction: a client must
    # never be handed two ids that could drift apart without notice.
    assert (snap.labware_id, snap.labware_name) == (snap.id, snap.name)
