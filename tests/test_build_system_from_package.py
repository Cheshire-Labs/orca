"""`build_system_from_package` builds a system from `topology.py` plus `workflows/*.py`.

Each test writes a small package to disk and imports it, because the helper's
job is the import: namespace packages with no `__init__.py`, a `workflows`
folder that may be missing, and the device factory the caller passes.
"""

from orca.events.event_bus import EventBus
from cheshire_drivers import AccessConfig, CartesianCoordinates, Teachpoint

import importlib
import os
import pathlib
import textwrap

import pytest
from cheshire_drivers.interfaces import BaseDriver
from cheshire_drivers.sims import SimShakerDriver

from orca.devices.shaker import Shaker
from orca.gateway.gateway_backed_driver import GatewayBackedDriver
from tests.memory_activity import memory_labware_service
from tests.memory_activity import memory_unsaved_positions
from tests.real_device_path import the_real_device_path
from orca.driver_management.device_factory_protocol import DeviceDriver
from orca.runtime.startup import PackageSetupStoreFactory
from orca.sdk.build import SystemBuild
from orca.sdk.package import build_system_from_package, build_topology_from_module
from tests.setup_package import package_stores, write_teachpoints
from tests.memory_activity import memory_ops_store


_TOPOLOGY = '''
from cheshire_drivers.sims import SimShakerDriver
from orca.devices.sealer import Sealer
from orca.devices.shaker import Shaker
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.sdk.build import Topology

{header}


class DeclaredShaker(SimShakerDriver):
    pass


def build_topology():
    shaker = Shaker("shaker_1")
    sealer = Sealer("sealer_1")
    arm = Transporter("arm")
    return Topology(
        locations={{"shaker_1": shaker, "sealer_1": sealer, "pad_1": PlatePad("pad_1")}},
        transporters=[arm],
    )
'''

_WORKFLOW = '''
import orca.orca as orca
from orca.devices.shaker import Shaker
from orca.sdk.labware import PlateTemplate

plate = PlateTemplate("{plate}", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
{extra}


def build_workflow(topology):
    shaker = topology.device("shaker_1", Shaker)

    @orca.action(device=shaker, inputs=[plate])
    async def shake(ctx):
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def shake_step(ctx):
        yield shake

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def plate_journey(ctx):
        yield shake_step

    @orca.workflow(name="{name}")
    def workflow(wf):
        wf.start(plate_journey)

    return workflow
'''

def _write_package(
    root: pathlib.Path,
    name: str,
    *,
    header: str = 'SYSTEM_NAME = "Tiny Bench"\nSYSTEM_DESCRIPTION = "two devices and a pad"',
    workflows: dict[str, str] | None = None,
) -> None:
    package = root / name
    package.mkdir(parents=True)
    (package / "topology.py").write_text(_TOPOLOGY.format(header=header))
    stores = package_stores(package)
    access = AccessConfig(name="vertical", access_type="vertical")
    write_teachpoints(stores, "arm", [
        Teachpoint(name, CartesianCoordinates(x, 0, 0, 0, 90, 180), orientation="right", access=access)
        for x, name in ((0, "shaker_1"), (100, "sealer_1"), (200, "pad_1"))
    ])
    if workflows is None:
        return
    (package / "workflows").mkdir()
    for stem, source in workflows.items():
        (package / "workflows" / f"{stem}.py").write_text(source)


def _stores_of(package_name: str) -> PackageSetupStoreFactory:
    spec = importlib.util.find_spec(package_name)
    assert spec is not None and spec.submodule_search_locations
    return package_stores(pathlib.Path(list(spec.submodule_search_locations)[0]))


def _workflow(name: str, *, plate: str = "plate_a", extra: str = "") -> str:
    return _WORKFLOW.format(name=name, plate=plate, extra=extra)


def _driver(build: SystemBuild, device_name: str) -> BaseDriver:
    return build.system.get_device(device_name).driver


@pytest.fixture
def package_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


async def test_workflow_files_register_in_file_order_and_private_ones_are_skipped(
    package_root: pathlib.Path,
) -> None:
    _write_package(package_root, "pkg_two_workflows", workflows={
        "b_second": _workflow("b_second", plate="plate_b"),
        "a_first": _workflow("a_first"),
        "_helper": "raise AssertionError('a _ module must not be imported')\n",
        "no_builder": "NOT_A_WORKFLOW = True\n",
    })

    build = await build_system_from_package("pkg_two_workflows", _stores_of("pkg_two_workflows"), memory_ops_store(), the_real_device_path().device_factory, event_bus=EventBus(), labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions())

    assert list(build.system.get_workflow_templates()) == ["a_first", "b_second"]
    assert build.workflow is None
    assert build.system.name == "Tiny Bench"
    assert build.system.description == "two devices and a pad"


async def test_a_package_with_no_workflows_folder_builds_with_zero_workflows(
    package_root: pathlib.Path,
) -> None:
    _write_package(package_root, "pkg_bare", header="")

    build = await build_system_from_package("pkg_bare", _stores_of("pkg_bare"), memory_ops_store(), the_real_device_path().device_factory, event_bus=EventBus(), labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions())

    assert list(build.system.get_workflow_templates()) == []
    assert build.system.name == "pkg_bare"
    assert build.system.description == ""


async def test_a_workflow_module_that_fails_to_import_is_not_read_as_no_workflows(
    package_root: pathlib.Path,
) -> None:
    _write_package(package_root, "pkg_broken_import", workflows={
        "broken": "import no_such_module_for_this_test\n",
    })

    with pytest.raises(ModuleNotFoundError, match="no_such_module_for_this_test"):
        await build_system_from_package("pkg_broken_import", _stores_of("pkg_broken_import"), memory_ops_store(), the_real_device_path().device_factory, event_bus=EventBus(), labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions())


async def test_reservoirs_declared_on_a_workflow_module_become_system_labware(
    package_root: pathlib.Path,
) -> None:
    extra = textwrap.dedent('''
        trough = PlateTemplate("trough", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
        reservoirs = [trough]
    ''')
    _write_package(package_root, "pkg_reservoirs", workflows={
        "with_trough": _workflow("with_trough", extra=extra),
    })

    build = await build_system_from_package("pkg_reservoirs", _stores_of("pkg_reservoirs"), memory_ops_store(), the_real_device_path().device_factory, event_bus=EventBus(), labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions())

    assert [lt.name for lt in build.system.labware_templates] == ["trough", "plate_a"]


class _OneShakerDriver:
    """A shaker gets one recorded driver; every other device gets the real device path's forwarding driver."""

    def __init__(self) -> None:
        self.shaker = SimShakerDriver("recorded")
        self._sim = the_real_device_path().device_factory

    def build_driver(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> DeviceDriver:
        if device_type == "shaker":
            return self.shaker
        return self._sim.build_driver(device_type, name, deck_modeling=deck_modeling)


async def test_the_factory_passed_in_supplies_every_device_its_drivers(
    package_root: pathlib.Path,
) -> None:
    _write_package(package_root, "pkg_factory", header="")
    factory = _OneShakerDriver()

    build = await build_system_from_package(
        "pkg_factory", _stores_of("pkg_factory"), memory_ops_store(), factory, event_bus=EventBus(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )

    assert _driver(build, "shaker_1") is factory.shaker
    assert isinstance(_driver(build, "sealer_1"), GatewayBackedDriver)


def test_build_topology_from_module_returns_the_module_topology(
    package_root: pathlib.Path,
) -> None:
    _write_package(package_root, "pkg_module_only", header="")
    module = importlib.import_module("pkg_module_only.topology")

    topology = build_topology_from_module(module)

    assert isinstance(topology.device("shaker_1", Shaker), Shaker)


async def test_one_folder_listed_under_two_sys_path_spellings_is_one_folder(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`python -m` lists the working directory as "" next to its absolute path."""
    _write_package(tmp_path, "pkg_two_spellings")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.syspath_prepend(os.path.join(str(tmp_path), "."))
    package = importlib.import_module("pkg_two_spellings")
    assert len(list(package.__path__)) == 2, list(package.__path__)

    build = await build_system_from_package("pkg_two_spellings", _stores_of("pkg_two_spellings"), memory_ops_store(), the_real_device_path().device_factory, event_bus=EventBus(), labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions())

    assert build.system.name == "Tiny Bench"


async def test_two_folders_of_the_same_name_on_sys_path_are_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path / "first", "pkg_dup")
    _write_package(tmp_path / "second", "pkg_dup")
    monkeypatch.syspath_prepend(str(tmp_path / "first"))
    monkeypatch.syspath_prepend(str(tmp_path / "second"))

    with pytest.raises(ImportError, match="more than one folder"):
        await build_system_from_package("pkg_dup", _stores_of("pkg_dup"), memory_ops_store(), the_real_device_path().device_factory, event_bus=EventBus(), labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions())
