"""`global_variables.json` in the deployment package declares global variables: type, default and bounds.

It also keeps the value an operator set, which must fit the declaration.
"""
import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.runtime.startup import PackageSetupStoreFactory
from orca.sdk.build import SystemBuild, Topology
from orca.variables.errors import VariableValidationError
from orca.runtime.setup_files import SetupFileError
from tests.memory_activity import memory_ops_store
from tests.setup_package import package_stores


def _write_global_variables(stores: PackageSetupStoreFactory, text: str) -> None:
    (stores.root / "global_variables.json").write_text(text, encoding="utf-8")


async def _build(stores: PackageSetupStoreFactory) -> SystemBuild:
    return await orca.build_system(
        name="globals", topology=Topology(locations={}, transporters=[], pools=[]), stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )


async def test_a_package_with_no_global_variables_file_declares_none() -> None:
    assert await package_stores().global_variables().read() == {}


async def test_a_declared_global_reads_its_default() -> None:
    stores = package_stores()
    _write_global_variables(stores, '{"reagent_batch": {"type": "str", "default": "B-001"}}')

    build = await _build(stores)

    assert build.system.variable_store.resolve_global("reagent_batch") == "B-001"


async def test_a_value_outside_a_declared_global_s_bounds_is_refused() -> None:
    stores = package_stores()
    _write_global_variables(stores, '{"plate_count": {"type": "int", "default": 1, "min": 1, "max": 4}}')

    build = await _build(stores)

    with pytest.raises(VariableValidationError):
        build.system.variable_store.set_global("plate_count", 9)


async def test_a_misspelled_field_is_refused_and_names_the_file() -> None:
    stores = package_stores()
    _write_global_variables(stores, '{"plate_count": {"type": "int", "defualt": 1}}')

    with pytest.raises(SetupFileError, match="global_variables.json"):
        await stores.global_variables().read()


async def test_a_default_outside_its_own_bounds_is_refused_and_names_the_file() -> None:
    stores = package_stores()
    _write_global_variables(stores, '{"plate_count": {"type": "int", "default": 9, "min": 1, "max": 4}}')

    with pytest.raises(SetupFileError, match="global_variables.json"):
        await stores.global_variables().read()


async def test_a_default_of_the_wrong_type_is_refused() -> None:
    stores = package_stores()
    _write_global_variables(stores, '{"plate_count": {"type": "int", "default": "abc"}}')

    with pytest.raises(SetupFileError, match="plate_count"):
        await stores.global_variables().read()


async def test_a_kept_value_a_later_edit_puts_out_of_bounds_is_refused_and_names_the_file() -> None:
    stores = package_stores()
    _write_global_variables(stores, '{"plate_count": {"type": "int", "default": 1, "max": 2, "value": 3}}')

    with pytest.raises(SetupFileError, match="global_variables.json"):
        await stores.global_variables().read()


async def test_an_entry_with_only_a_description_and_a_value_is_not_a_declaration() -> None:
    stores = package_stores()
    _write_global_variables(stores, '{"operator": {"description": "who runs it", "value": 3}}')

    build = await _build(stores)

    assert build.system.variable_store.resolve_global("operator") == 3
