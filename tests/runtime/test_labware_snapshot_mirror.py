"""The wire model mirrors the snapshot, field for field.

Every read surface reports labware through ``LabwareSnapshotModel``, which is
hand-written rather than derived. A field added to the snapshot and forgotten
here is invisible on every surface while looking entirely healthy, which is how
a carry override could be set and never seen again.
"""

import dataclasses

from orca.operations.labware_models import LabwareSnapshotModel
from orca.runtime.status_models import LabwareSnapshot


def test_the_wire_model_carries_every_snapshot_field() -> None:
    snapshot_fields = {f.name for f in dataclasses.fields(LabwareSnapshot)}

    assert snapshot_fields <= set(LabwareSnapshotModel.model_fields)
