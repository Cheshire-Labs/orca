"""Resolve `LabwareDefinition` entries to labware factories.

This module separates "what labware is this?" (catalog lookup, owned by
`ILabwareCatalog`) from "how do I instantiate it?" (factory dispatch,
owned here). `PlateTemplate.create_instance()` and its siblings
ask the catalog for a definition, then ask this resolver to turn it into a
callable. Keeping the resolver here means `labware.py` stays free of any
direct `pylabrobot` or `cheshire_drivers.plr` knowledge.

The resolver looks up factories in `cheshire_drivers.plr.plates`
(the curated module that wraps raw PLR factories with `PLRPlateAdapter`
/ `PLRTipRackAdapter` / `PLRTroughAdapter`, satisfying the `IPlate` /
`ITipRack` / `ITrough` contracts orca-core templates expect). Raw PLR
factories are NOT used here because they return concrete PLR types that
do not directly satisfy those interfaces.
"""

from typing import Any, Callable

from cheshire_drivers.labware_seed import LabwareSeedEntry
from cheshire_drivers.plr import plates as cd_plr_factories


def resolve_plr_factory(definition: LabwareSeedEntry) -> Callable[..., Any]:
    """Return the orca-core-compatible factory callable for a seeded labware definition.

    Looks up `definition.labware_type` in `cheshire_drivers.plr.plates`
    (the curated wrapped-factory module). Raises `RuntimeError` if not
    found: a labware_type referenced by a workflow must have a matching
    wrapped factory in the cheshire-drivers PLR layer.

    Raises `NotImplementedError` for operator-custom rows (`plr_class_name
    is None`) -- those need geometry-based construction via
    `cheshire_drivers.plr.labware_converter.PLRLabwareConverter`, which is
    not wired into the template path yet.
    """
    if definition.plr_class_name is None:
        raise NotImplementedError(
            f"Labware {definition.labware_type!r} has no plr_class_name; "
            "operator-custom labware (geometry-only) cannot be built via "
            "the template path yet -- use PLRLabwareConverter directly."
        )
    factory = getattr(cd_plr_factories, definition.labware_type, None)
    if factory is None:
        raise RuntimeError(
            f"Labware factory {definition.labware_type!r} not found in "
            "cheshire_drivers.plr.plates; the seed catalog references a "
            "labware_type that the cheshire-drivers wheel does not expose "
            "as a wrapped factory. Either add a wrapper or fix the seed."
        )
    return factory
