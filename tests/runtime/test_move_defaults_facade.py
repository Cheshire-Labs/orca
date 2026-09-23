"""Reading and editing what an arm's moves start from, through the facade."""

import pytest
from cheshire_drivers.move_parameters import SEED_MOVE_PARAMETERS, MoveParameterPatch

from orca.runtime.danger import ConfirmationRequired
from orca.runtime.db import create_memory_engine
from orca.runtime.facades.move_defaults import MoveDefaultsFacade
from orca.runtime.move_defaults_service import MoveDefaultsService
from orca.runtime.sqlite_move_defaults_store import SqliteMoveDefaultsStore


def _facade() -> MoveDefaultsFacade:
    return MoveDefaultsFacade(
        MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine())),
    )


@pytest.mark.asyncio
async def test_an_arm_nobody_tuned_reads_as_the_seed_and_says_so() -> None:
    record = await _facade().get("pf400")

    assert record.parameters == SEED_MOVE_PARAMETERS
    assert set(record.sources.values()) == {"seed"}


@pytest.mark.asyncio
async def test_reading_does_not_create_a_row() -> None:
    """Seeding on read would make "nobody has tuned this arm" and "somebody set it
    to exactly the seed" the same answer, and only one of them is safe to change."""
    service = MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine()))
    facade = MoveDefaultsFacade(service)

    await facade.get("pf400")

    assert await service.get("pf400") is None


@pytest.mark.asyncio
async def test_an_edit_names_one_field_and_leaves_the_rest_on_the_seed() -> None:
    facade = _facade()

    record = await facade.apply(
        "pf400", MoveParameterPatch(travel_margin=25.0), confirm=True,
    )

    assert record.parameters.travel_margin == 25.0
    assert record.sources["travel_margin"] == "defaults"
    assert record.parameters.jaw_opening == SEED_MOVE_PARAMETERS.jaw_opening
    assert record.sources["jaw_opening"] == "seed"


@pytest.mark.asyncio
async def test_a_second_edit_keeps_what_the_first_one_set() -> None:
    facade = _facade()
    await facade.apply("pf400", MoveParameterPatch(travel_margin=25.0), confirm=True)

    record = await facade.apply(
        "pf400", MoveParameterPatch(jaw_opening=18.0), confirm=True,
    )

    assert record.parameters.travel_margin == 25.0
    assert record.parameters.jaw_opening == 18.0


@pytest.mark.asyncio
async def test_clearing_a_field_puts_it_back_on_the_seed() -> None:
    facade = _facade()
    await facade.apply(
        "pf400",
        MoveParameterPatch(travel_margin=25.0, jaw_opening=18.0),
        confirm=True,
    )

    record = await facade.apply(
        "pf400", MoveParameterPatch(), clear=["travel_margin"], confirm=True,
    )

    assert record.parameters.travel_margin == SEED_MOVE_PARAMETERS.travel_margin
    assert record.sources["travel_margin"] == "seed"
    assert record.sources["jaw_opening"] == "defaults"


@pytest.mark.asyncio
async def test_clearing_the_last_tuned_field_leaves_no_row_behind() -> None:
    """An empty row and no row mean the same thing, and keeping both around makes
    the tuned-arm list report arms nobody has tuned."""
    service = MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine()))
    facade = MoveDefaultsFacade(service)
    await facade.apply("pf400", MoveParameterPatch(travel_margin=25.0), confirm=True)

    await facade.apply(
        "pf400", MoveParameterPatch(), clear=["travel_margin"], confirm=True,
    )

    assert await service.get("pf400") is None


@pytest.mark.asyncio
async def test_resetting_drops_everything_the_deployment_tuned() -> None:
    facade = _facade()
    await facade.apply("pf400", MoveParameterPatch(travel_margin=25.0), confirm=True)

    assert await facade.reset("pf400", confirm=True) is True
    assert (await facade.get("pf400")).parameters == SEED_MOVE_PARAMETERS
    assert await facade.reset("pf400", confirm=True) is False


@pytest.mark.asyncio
async def test_an_edit_without_confirmation_is_refused() -> None:
    """These numbers drive an arm into a nest; an accidental call must not land."""
    with pytest.raises(ConfirmationRequired):
        await _facade().apply("pf400", MoveParameterPatch(clearance=0.0))


@pytest.mark.asyncio
async def test_a_reset_without_confirmation_is_refused() -> None:
    with pytest.raises(ConfirmationRequired):
        await _facade().reset("pf400")


@pytest.mark.asyncio
async def test_a_number_the_position_decides_is_refused_not_stored() -> None:
    """Every teachpoint supplies the approach numbers, so an arm-wide value for one
    would be stored, reported back as this deployment's choice, and never read by
    any move."""
    facade = _facade()

    for field, value in (
        ("access_type", "horizontal"), ("clearance", 45.0),
        ("z_above", 5.0), ("grasp_offset", 3.0),
    ):
        with pytest.raises(ValueError, match="access config"):
            await facade.apply(
                "pf400", MoveParameterPatch.model_validate({field: value}),
                confirm=True,
            )


@pytest.mark.asyncio
async def test_clearing_a_number_the_position_decides_is_refused_too() -> None:
    """Nothing can put one there, so a clear for one is an operator working from
    the wrong model and is better told than quietly obliged."""
    with pytest.raises(ValueError, match="access config"):
        await _facade().apply(
            "pf400", MoveParameterPatch(), clear=["clearance"], confirm=True,
        )


@pytest.mark.asyncio
async def test_a_refused_edit_stores_nothing() -> None:
    service = MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine()))
    facade = MoveDefaultsFacade(service)

    with pytest.raises(ValueError):
        await facade.apply(
            "pf400",
            MoveParameterPatch(travel_margin=25.0, clearance=45.0),
            confirm=True,
        )

    assert await service.get("pf400") is None


@pytest.mark.asyncio
async def test_the_list_holds_the_arms_somebody_tuned() -> None:
    facade = _facade()
    await facade.apply("pf400", MoveParameterPatch(travel_margin=25.0), confirm=True)

    records = await facade.list()

    assert [r.transporter_name for r in records] == ["pf400"]


@pytest.mark.asyncio
async def test_a_mounted_system_puts_its_untuned_arms_in_the_list_too() -> None:
    """An arm missing from the list reads as an arm that does not exist, and the
    ones an operator most needs to find are the ones nobody has tuned yet."""
    from tests.test_system_runtime import _build_simple_system

    system, _ = await _build_simple_system()
    facade = _facade()

    records = await facade.list(system)

    assert [r.transporter_name for r in records] == ["robot1"]
    assert set(records[0].sources.values()) == {"seed"}
