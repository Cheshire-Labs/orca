"""A build reads each setup file the same number of times however many entries it holds."""

from orca.events.event_bus import EventBus
import json
from collections import Counter
from pathlib import Path

import pytest
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint

from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime import setup_files
from orca.runtime.setup_file_stores import TeachpointFileStore
from orca.runtime.setup_files import SetupFileError
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.package import build_system
from tests.setup_package import package_stores, write_teachpoints
from tests.memory_activity import memory_ops_store


def _taught(position_id: str) -> Teachpoint:
    return Teachpoint(
        position_id, CartesianCoordinates(1, 2, 3, 0, 90, 180), orientation="right",
        access=AccessConfig(name="vert", access_type="vertical"),
    )


async def _build_with_pads(root: Path, pads: int, untaught: tuple[str, ...] = ()) -> SystemBuild:
    names = [f"pad_{i}" for i in range(pads)]
    stores = package_stores(root)
    write_teachpoints(stores, "arm", [_taught(name) for name in names])
    return await build_system(
        "reads",
        Topology(
            locations={name: PlatePad(name) for name in [*names, *untaught]},
            transporters=[Transporter("arm")],
            pools=[],
        ),
        stores, event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )


def _count_reads(monkeypatch: pytest.MonkeyPatch, root: Path) -> Counter[str]:
    reads: Counter[str] = Counter()
    real_read = setup_files.read_setup_text

    def counted(path: Path) -> str:
        reads[path.resolve().relative_to(root.resolve()).as_posix()] += 1
        return real_read(path)

    monkeypatch.setattr(setup_files, "read_setup_text", counted)
    return reads


async def test_file_reads_do_not_grow_with_the_number_of_teachpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    small_root, large_root = tmp_path / "small", tmp_path / "large"
    # Not monkeypatch.undo(): that would also drop the temp package folder every test is given.
    with monkeypatch.context() as counting:
        small_reads = _count_reads(counting, small_root)
        await _build_with_pads(small_root, pads=3)
    large_reads = _count_reads(monkeypatch, large_root)
    await _build_with_pads(large_root, pads=12)

    assert small_reads == large_reads
    assert sum(small_reads.values()) > 0


async def test_a_transporter_observes_every_taught_location_and_no_other(tmp_path: Path) -> None:
    build = await _build_with_pads(tmp_path, pads=3, untaught=("not_taught",))
    system_map = build.system.system_map
    arm = build.system.get_transporter("arm")

    assert all(
        arm in system_map.get_location(f"pad_{i}")._labware_observers for i in range(3)
    )
    assert arm not in system_map.get_location("not_taught")._labware_observers


async def test_a_location_added_after_the_build_is_hooked_when_taught(tmp_path: Path) -> None:
    build = await _build_with_pads(tmp_path, pads=2)
    arm = build.system.get_transporter("arm")
    stores = package_stores(tmp_path)
    write_teachpoints(stores, "arm", [_taught("pad_0"), _taught("pad_1"), _taught("late")])

    await build.system.add_location(Location("late", PlatePad("late")))

    assert arm in build.system.system_map.get_location("late")._labware_observers


async def _reads_teaching_one_more_position(
    root: Path, monkeypatch: pytest.MonkeyPatch, pads: int,
) -> Counter[str]:
    build = await _build_with_pads(root, pads=pads)
    system_map = build.system.system_map
    arm = build.system.get_transporter("arm")
    await build.system.add_location(Location("late", PlatePad("late")))
    write_teachpoints(
        package_stores(root), "arm",
        [*(_taught(f"pad_{i}") for i in range(pads)), _taught("late")],
    )
    with monkeypatch.context() as counting:
        reads = _count_reads(counting, root)
        await system_map.add_taught_position(arm, "late")

    assert arm in system_map.get_location("late")._labware_observers
    return reads


async def test_teaching_a_position_reads_the_same_however_many_positions_are_taught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    small = await _reads_teaching_one_more_position(tmp_path / "small", monkeypatch, pads=2)
    large = await _reads_teaching_one_more_position(tmp_path / "large", monkeypatch, pads=8)

    assert small == large
    assert sum(small.values()) > 0


def _write_teachpoint_naming(path: Path, position_id: str, access: str) -> None:
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    entries[position_id] = {
        "access": access, "orientation": "right",
        "cartesian": {"x": 1, "y": 2, "z": 3, "yaw": 0, "pitch": 90, "roll": 180},
    }
    path.write_text(json.dumps(entries), encoding="utf-8")


async def test_listing_reads_the_access_config_file_once_however_many_configs_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = tmp_path / "access_configs.json"
    names = [f"config_{i}" for i in range(5)]
    configs.write_text(
        json.dumps({name: {"name": name, "access_type": "vertical"} for name in names}),
        encoding="utf-8",
    )
    path = tmp_path / "arm.json"
    for name in names:
        _write_teachpoint_naming(path, f"pad_{name}", name)
    teachpoints = TeachpointFileStore(path, configs)
    reads = _count_reads(monkeypatch, tmp_path)

    await teachpoints.list()

    assert reads["access_configs.json"] == 1


async def test_listing_refuses_a_teachpoint_naming_a_missing_access_config(tmp_path: Path) -> None:
    path = tmp_path / "arm.json"
    path.write_text('{"pad_1": {"access": "gone"}}', encoding="utf-8")

    with pytest.raises(SetupFileError, match="gone"):
        await TeachpointFileStore(path, tmp_path / "access_configs.json").list()
