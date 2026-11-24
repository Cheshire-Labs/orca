"""Guard: one module owns the physical facts, and nothing else stores them.

The bug this epic exists to kill is a fact kept in two places. Two stores drift,
and a plate ends up at two positions at once. Three rules keep that from coming
back, checked as source TEXT (the repo bans parsing source into a syntax tree).

1. Nothing outside `orca/state/` keeps a position of its own. A holder that
   grows a `_labware` field back is the exact regression.
2. Only the two units allowed to speak for a fact hold the ledger, and only the
   system build makes one. Everything else asks a service.
3. `orca/state/` imports no topology and no execution model. That is what keeps
   it testable on its own and unable to form an import cycle.

What rule 1 does NOT do: it names the fields that have meant occupancy in this
tree, so a holder that invents `self._occupant` or a dict of its own passes
clean. It stops the regression, not every possible second store.

Both controls are here on purpose: a text scanner over a clean tree passes
whether or not it works, so a positive control proves it fires and a negative
control proves it does not fire on the legitimate shape.
"""

import re
from pathlib import Path

import pytest

from cheshire_source_text import code_lines, code_lines_of, imported_modules, python_files

_SRC = Path(__file__).resolve().parents[1] / "src" / "orca"
_STATE = _SRC / "state"

# Names that only ever mean "this position's occupant". A holder growing one
# of these back is the regression, wherever it lives.
_OCCUPANCY_FIELD = re.compile(
    r"self\._(loaded_labware|stage_labware|staged_labware)\s*(?::[^=]+)?=(?!=)"
)

# `_labware` means the occupant only inside a position holder. Elsewhere it is
# the labware a thread, route or action is about, which is a reference and not
# a store, so the file has to declare itself a holder first.
_HOLDER_FIELD = re.compile(r"self\._labware\s*(?::[^=]+)?=(?!=)")
_IS_A_HOLDER = re.compile(r"\bILabwarePlaceable\b")

# Reaching the ledger itself rather than asking something that owns it: naming
# its type, or taking it off the `LabwarePlacements` that carries it.
_LEDGER_REACH = re.compile(r"\bPlacementLedger\b|\.ledger\b")

# The units allowed to name the ledger, and why:
#   position_occupancy -- the holders' one occupancy implementation
#   labware_location_service -- the topology-aware adapter over the ledger
#   labware_directory -- bundles the ledger with the labware its refs name
#   SdkToSystemBuilder -- makes one ledger per system build
_LEDGER_CALLERS = {
    _SRC / "resource_models" / "position_occupancy.py",
    _SRC / "resource_models" / "labware_location_service.py",
    _SRC / "resource_models" / "labware_directory.py",
    _SRC / "system" / "SdkToSystemBuilder.py",
}

# What `orca/state/` may not know about.
_FORBIDDEN_IN_STATE = (
    "orca.resource_models.location",
    "orca.resource_models.devices",
    "orca.resource_models.labware",
    "orca.system",
    "orca.workflow_models",
    "orca.devices",
    "orca.runtime.system_runtime",
    "orca.runtime.facades",
)


def _orca_files() -> list[Path]:
    return [p for p in python_files(_SRC) if _STATE not in p.parents]


def test_no_module_outside_state_stores_a_physical_fact() -> None:
    offenders = []
    for path in _orca_files():
        lines = code_lines_of(path)
        is_holder = any(_IS_A_HOLDER.search(line.text) for line in lines)
        for line in lines:
            hit = _OCCUPANCY_FIELD.search(line.text) or (
                is_holder and _HOLDER_FIELD.search(line.text)
            )
            if hit:
                offenders.append(f"{path.relative_to(_SRC)}:{line.lineno}: {line.text.strip()}")
    assert not offenders, (
        "a physical fact is being stored outside orca/state/; ask the ledger "
        "instead of keeping a second copy:\n" + "\n".join(offenders)
    )


def test_only_its_owner_reaches_the_ledger() -> None:
    offenders = []
    for path in _orca_files():
        if path in _LEDGER_CALLERS:
            continue
        for line in code_lines_of(path):
            if _LEDGER_REACH.search(line.text):
                offenders.append(f"{path.relative_to(_SRC)}:{line.lineno}: {line.text.strip()}")
    assert not offenders, (
        "the placement ledger is reached outside the units that speak for it; "
        "ask the location service:\n" + "\n".join(offenders)
    )


def test_the_state_module_knows_nothing_of_the_topology() -> None:
    offenders = []
    for path in python_files(_STATE):
        for module in imported_modules(code_lines_of(path)):
            if any(module.module.startswith(f) for f in _FORBIDDEN_IN_STATE):
                offenders.append(f"{path.name}: imports {module.module}")
    assert not offenders, (
        "orca/state/ imported the topology or the execution model; it answers "
        "in identities so it never has to:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("shape", [
    "self._loaded_labware: list[LabwareInstance] = []",
    "self._staged_labware = None",
    "self._stage_labware = labware",
])
def test_the_occupancy_scan_fires_on_the_shape_it_claims(shape: str) -> None:
    """Positive control. Without it a broken regex passes on a clean tree."""
    assert _OCCUPANCY_FIELD.search(code_lines(shape)[0].text)


def test_the_holder_marker_fires_on_a_holder_class() -> None:
    assert _IS_A_HOLDER.search(code_lines("class PlatePad(IResource, ILabwarePlaceable):")[0].text)


def test_the_holder_scan_fires_on_a_holder_storing_its_occupant() -> None:
    assert _HOLDER_FIELD.search(code_lines("self._labware = labware")[0].text)


@pytest.mark.parametrize("shape", [
    "self._labware_contents = LabwareContentsLedger(ops_history)",
    "self._labware_registry = registry",
    "self._mounted_tips = MountedTipsLedger(ops_history)",
    "occupant = self._occupancy.labware",
    "if self._labware == labware:",
])
def test_the_scans_leave_the_legitimate_shape_alone(shape: str) -> None:
    """Negative control: ledger handles, a registry, a read, a comparison."""
    text = code_lines(shape)[0].text
    assert not _OCCUPANCY_FIELD.search(text)
    assert not _HOLDER_FIELD.search(text)


@pytest.mark.parametrize("shape", [
    "self._ledger: PlacementLedger | None = None",
    "held = placements.ledger.placement_of(ref)",
    "self._ledger = self._placements.ledger",
])
def test_the_ledger_scan_fires_on_every_reach(shape: str) -> None:
    assert _LEDGER_REACH.search(code_lines(shape)[0].text)


@pytest.mark.parametrize("shape", [
    "where = self._labware_location_service.get(labware)",
    "position.bind_placements(self._placements)",
    "await ledger.seed_at_birth(self.ref, declared)",
    "self._labware_contents = LabwareContentsLedger(ops_history)",
])
def test_the_ledger_scan_leaves_the_legitimate_shapes_alone(shape: str) -> None:
    assert not _LEDGER_REACH.search(code_lines(shape)[0].text)
