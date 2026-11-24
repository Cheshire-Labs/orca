"""`orca device compare-deck` prints a disagreement that names no labware without inventing a name."""

import pytest

from orca.cli import device
from orca.operations.device_models import DeckDisagreementDTO


def test_an_interrupted_move_of_nothing_on_record_prints_its_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(device.output, "info", lines.append)

    device._report_disagreements("flex_1", [DeckDisagreementDTO(
        labware_name=None, labware_id=None, reason="interrupted_move",
        ledger_site="flex_1/B2-slot", driver_site="B3-slot", detail="Look at the gripper.",
    )])

    assert lines[1] == "  no labware on record, flex_1/B2-slot to B3-slot (interrupted_move): Look at the gripper."
    assert "None" not in lines[1]
