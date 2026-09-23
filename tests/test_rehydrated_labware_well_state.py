"""A resident restored from a store must come back as the labware it was.

Labware stores persist identity, never the live PLR object, so a resident that
outlives a restart arrives knowing only its name, template and barcode. The
deck reconciliation on the next build asks every resident for its driver well
state, and the templates assert their concrete instance type there. The
assertion fired, ``build()`` never completed, and every route on the deployment
answered 503 until the persisted rows were deleted by hand.

Seen on the bench: one completed run left three residents behind, and the next
reload died with "TipRackTemplate resolves TipRackInstance only".

The runtime now hands each persisted labware back to its template to be rebuilt,
so what lands on the deck is the same concrete instance a fresh run would build,
under the persisted id and name. What it HOLDS comes from the record the
original wrote, never from the template again. Skipping the well state instead
(returning None) would leave the driver on its own defaults, and a PLR tip-rack
factory defaults to a FULL rack: a declared-empty resident would come back with
96 tips the record knows are not there.

A genuinely mismatched pairing -- a plate handed to a tip-rack template -- is
still a programming error and still asserts.
"""

from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import (
    LabwareInitialState,
    LabwareInstance,
    PlateInstance,
    TipRackInstance,
    TroughInstance,
)
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from tests.test_helpers import (
    FactoryPlateTemplate as PlateTemplate,
    FactoryTipRackTemplate as TipRackTemplate,
    FactoryTroughTemplate as TroughTemplate,
    bind_ledger,
)


async def _restored_with_its_record(template) -> LabwareInstance:
    """A fresh labware, its opening entry written, then rebuilt from a store row.

    The restart shape: the record outlives the instance, and the rebuilt one
    reads what the original wrote.
    """
    history = OpsHistory()
    fresh = await template.create_instance()
    await fresh.enter_record(bind_ledger(fresh, history))
    restored = await template.restore_instance(_persisted(fresh))
    restored.bind_contents(LabwareContentsLedger(history))
    return restored

pytestmark = pytest.mark.asyncio


def _plate_factory(num_rows: int = 2, num_cols: int = 2, well_max: float = 300.0):
    def factory(name: str, with_lid: bool | None = None):
        plate = MagicMock()
        plate.name = name
        plate.model = "test_plate"
        plate.barcode = None
        plate.num_rows = num_rows
        plate.num_cols = num_cols
        plate.well.side_effect = lambda identifier: MagicMock(max_volume=well_max)
        return plate
    return factory


def _trough_factory(max_volume: float = 50000.0):
    def factory(name: str):
        trough = MagicMock()
        trough.name = name
        trough.model = "test_trough"
        trough.max_volume = max_volume
        return trough
    return factory


def _tip_rack_factory(num_tips: int = 4):
    def factory(name: str, with_tips: bool):
        rack = MagicMock()
        rack.name = name
        rack.model = "test_rack"
        spots = []
        for identifier in ["A1", "A2", "B1", "B2"][:num_tips]:
            spot = MagicMock()
            spot.identifier = identifier
            spot.has_tip = with_tips
            spots.append(spot)
        rack.tip_spots.return_value = spots
        return rack
    return factory


def _persisted(instance: LabwareInstance) -> LabwareInstance:
    """What a store hands back after a restart: identity, no PLR object."""
    restored = LabwareInstance(
        template_name=instance.template_name,
        labware_type=instance.labware_type,
        barcode=instance.barcode,
        instance_id=instance.id,
        name=instance.name,
    )
    restored.metadata.update(instance.metadata)
    return restored


async def test_a_restored_tip_rack_keeps_its_declared_empty_layout() -> None:
    """The regression this whole path exists to prevent.

    A ``with_tips=False`` rack that sat on the deck through a restart must come
    back empty. Leaving the driver to its own default gives it a full rack, and
    the next pick_up_tips targets a spot the ledger knows has no tip.
    """
    template = TipRackTemplate("r", _tip_rack_factory(num_tips=4), with_tips=False)

    restored = await _restored_with_its_record(template)

    state = await restored.driver_well_state()
    assert state is not None and state.tips == {
        "A1": False, "A2": False, "B1": False, "B2": False,
    }


async def test_a_restored_tip_rack_keeps_its_declared_sparse_layout() -> None:
    template = TipRackTemplate(
        "r", _tip_rack_factory(num_tips=4), with_tips=True,
        initial_state=LabwareInitialState(tip_positions=["A1", "B2"]),
    )

    restored = await _restored_with_its_record(template)

    state = await restored.driver_well_state()
    assert state is not None and state.tips == {
        "A1": True, "A2": False, "B1": False, "B2": True,
    }


async def test_a_restored_trough_keeps_its_declared_fill_and_replenishment() -> None:
    template = TroughTemplate(
        "tr", _trough_factory(max_volume=9000.0),
        initial_state=LabwareInitialState(max_fill=True, replenished=True),
    )

    restored = await _restored_with_its_record(template)

    state = await restored.driver_well_state()
    assert state is not None
    assert state.volumes == {"A1": 9000.0}
    assert state.replenished is True


async def test_a_restored_labware_is_the_concrete_instance_again() -> None:
    """Everything downstream of the deck reads the PLR object: ``ctx.plate()``,
    the tip-depletion check, the move onto an LH deck. Identity has to survive
    the rebuild or the driver deck and the store name two different labwares.
    """
    template = PlateTemplate("p", _plate_factory())
    fresh = await template.create_instance()
    fresh.barcode = "BC-1234"
    fresh.metadata["lot"] = "L9"

    restored = await template.restore_instance(_persisted(fresh))

    assert isinstance(restored, PlateInstance)
    assert restored.has_plr_backing
    assert (restored.id, restored.name) == (fresh.id, fresh.name)
    assert restored.barcode == "BC-1234"
    assert restored.metadata["lot"] == "L9"
    assert restored.template is template


@pytest.mark.parametrize(
    "template_factory, instance_type",
    [
        (lambda: PlateTemplate("p", _plate_factory()), PlateInstance),
        (lambda: TipRackTemplate("r", _tip_rack_factory(), with_tips=True), TipRackInstance),
        (lambda: TroughTemplate("tr", _trough_factory()), TroughInstance),
    ],
)
async def test_every_plr_backed_template_restores_its_own_instance_type(
    template_factory, instance_type,
) -> None:
    template = template_factory()
    restored = await template.restore_instance(_persisted(await template.create_instance()))
    assert isinstance(restored, instance_type)


async def test_identity_only_labware_does_not_wedge_the_driver_projection() -> None:
    """The fallback for a labware nothing could rebuild (its template is gone
    from the system, or its labware_type left the catalog). A deck the driver
    models from its own defaults is wrong but recoverable; an assertion here
    takes every route on the deployment to 503.
    """
    template = TipRackTemplate("r", _tip_rack_factory(), with_tips=False)
    stranded = _persisted(await template.create_instance())
    bind_ledger(stranded, OpsHistory())

    assert await stranded.driver_well_state() is None


async def test_identity_only_labware_skips_its_initial_state_seed() -> None:
    """The other seeding entry point: a LIVE ``MANUAL_PLACE`` thread binds
    whatever the operator left on the pad, which after a restart can be a
    labware nothing rebuilt. Seeding it would assert and error the thread; its
    starting state is already in the ledger from the run that placed it.
    """
    template = PlateTemplate("p", _plate_factory())
    stranded = _persisted(await template.create_instance())
    stranded._template = template
    history = OpsHistory()

    await stranded.enter_record(bind_ledger(stranded, history))

    assert await history.all_operations() == []


async def test_a_plate_handed_to_a_tip_rack_template_still_asserts() -> None:
    """The guard is about a missing PLR object, not about type discipline: a
    real mismatch is still a programming error.
    """
    plate_template = PlateTemplate("p", _plate_factory())
    rack_template = TipRackTemplate("r", _tip_rack_factory(), with_tips=True)

    with pytest.raises(AssertionError):
        rack_template.declared_contents(await plate_template.create_instance())
