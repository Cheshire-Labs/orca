"""Build each example package in-process for tests, on the real device path. Every call makes fresh devices."""

import importlib
from pathlib import Path

from cheshire_drivers.device_setup import BUILTIN_CLIENT

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.runtime.startup import PackageSetupStoreFactory
from orca.sdk.build import SystemBuild
from tests.memory_activity import memory_ops_store
from tests.memory_activity import memory_unsaved_positions
from tests.real_device_path import the_real_device_path
from tests.memory_activity import memory_labware_service

SMC_WORKFLOW = "smc_assay"
HAMILTON_SMC_WORKFLOW = "hamilton_smc_assay"
OPENTRONS_FLEX_SMC_WORKFLOW = "opentrons_flex_smc_assay"
PLR_WORKFLOW = "learn_orca"
VENUS_WORKFLOW = "example_workflow"


def package_setup_stores(package_name: str) -> PackageSetupStoreFactory:
    """The setup stores of an importable example package, read from its folder."""
    package = importlib.import_module(package_name)
    return PackageSetupStoreFactory(Path(list(package.__path__)[0]), default_client=BUILTIN_CLIENT)


async def build_example(package: str, workflow_name: str) -> SystemBuild:
    """Build `examples.<package>` and point `build.workflow` at its named workflow."""
    package_name = f"examples.{package}"
    build = await orca.build_system_from_package(
        package_name, package_setup_stores(package_name), memory_ops_store(),
        the_real_device_path().device_factory, event_bus=EventBus(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    build.workflow = build.system.get_workflow_template(workflow_name)
    return build


async def build_smc() -> SystemBuild:
    return await build_example("smc_assay", SMC_WORKFLOW)


async def build_hamilton_smc() -> SystemBuild:
    return await build_example("hamilton_smc", HAMILTON_SMC_WORKFLOW)


async def build_opentrons_flex_smc() -> SystemBuild:
    return await build_example("opentrons_flex_smc", OPENTRONS_FLEX_SMC_WORKFLOW)


async def build_plr() -> SystemBuild:
    return await build_example("pylabrobot_example", PLR_WORKFLOW)


async def build_venus() -> SystemBuild:
    return await build_example("simple_venus_example", VENUS_WORKFLOW)
