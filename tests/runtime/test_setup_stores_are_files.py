"""Setup stores keep their entries in the package's JSON files."""

import json
from pathlib import Path

import pytest
from cheshire_drivers import DeckLayoutConfig
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint

from orca.runtime.access_config_store import AccessConfigInUseError
from orca.runtime.setup_file_stores import (
    AccessConfigFileStore,
    DeckLayoutFileStore,
    MoveDefaultsFileStore,
    TeachpointFileStore,
)
from orca.runtime.setup_files import SetupEntryExistsError, SetupFileError
from orca.runtime.startup import PackageSetupStoreFactory
from cheshire_drivers.device_setup import BUILTIN_CLIENT


def _pad(access: AccessConfig) -> Teachpoint:
    return Teachpoint(
        "pad_1", CartesianCoordinates(1, 2, 3, 0, 90, 180), orientation="right", access=access,
    )


async def test_a_teachpoint_names_its_access_config_instead_of_copying_it(tmp_path: Path) -> None:
    access = AccessConfigFileStore(tmp_path / "access_configs.json")
    await access.add(AccessConfig(name="vert", access_type="vertical", vertical_clearance=20.0))
    teachpoints = TeachpointFileStore(tmp_path / "teachpoints" / "arm_1.json", tmp_path / "access_configs.json")
    await teachpoints.add(_pad(AccessConfig(name="vert", access_type="vertical")))

    on_disk = json.loads((tmp_path / "teachpoints" / "arm_1.json").read_text(encoding="utf-8"))
    assert on_disk["pad_1"]["access"] == "vert"
    assert "vertical_clearance" not in on_disk["pad_1"]

    await access.update(AccessConfig(name="vert", access_type="vertical", vertical_clearance=55.0))
    read = await teachpoints.get("pad_1")
    assert read is not None
    assert read.vertical_clearance == 55.0
    assert read.coordinates == CartesianCoordinates(1, 2, 3, 0, 90, 180)


async def test_a_hand_edit_naming_a_missing_access_config_is_refused_on_read(tmp_path: Path) -> None:
    access = AccessConfigFileStore(tmp_path / "access_configs.json")
    (tmp_path / "arm_1.json").write_text(
        json.dumps({"pad_1": {"access": "gone", "orientation": "right"}}), encoding="utf-8",
    )
    teachpoints = TeachpointFileStore(tmp_path / "arm_1.json", tmp_path / "access_configs.json")

    with pytest.raises(SetupFileError, match="gone"):
        await teachpoints.get("pad_1")


async def test_orca_never_writes_a_teachpoint_naming_a_missing_access_config(tmp_path: Path) -> None:
    access = AccessConfigFileStore(tmp_path / "access_configs.json")
    teachpoints = TeachpointFileStore(tmp_path / "arm_1.json", tmp_path / "access_configs.json")

    with pytest.raises(KeyError, match="gone"):
        await teachpoints.add(_pad(AccessConfig(name="gone", access_type="vertical")))
    assert not (tmp_path / "arm_1.json").exists()


async def test_add_refuses_a_duplicate_and_update_refuses_an_unknown(tmp_path: Path) -> None:
    access = AccessConfigFileStore(tmp_path / "access_configs.json")
    await access.add(AccessConfig(name="vert", access_type="vertical"))

    with pytest.raises(SetupEntryExistsError, match="vert"):
        await access.add(AccessConfig(name="vert", access_type="vertical"))
    with pytest.raises(KeyError, match="horiz"):
        await access.update(AccessConfig(name="horiz", access_type="horizontal"))
    assert await access.delete("vert") is True
    assert await access.delete("vert") is False


async def test_deck_layouts_and_move_settings_round_trip(tmp_path: Path) -> None:
    decks = DeckLayoutFileStore(tmp_path / "deck_layouts" / "lh_1.json")
    await decks.add("default", DeckLayoutConfig(deck_type="FlexDeck", resources=[]))
    defaults = MoveDefaultsFileStore(tmp_path / "move_defaults.json")
    await defaults.set("arm_1", MoveParameterPatch(speed=40.0))

    assert await decks.list() == [("default", DeckLayoutConfig(deck_type="FlexDeck", resources=[]))]
    assert await MoveDefaultsFileStore(tmp_path / "move_defaults.json").get("arm_1") == MoveParameterPatch(speed=40.0)


async def test_an_access_config_a_teachpoint_names_cannot_be_deleted(tmp_path: Path) -> None:
    stores = PackageSetupStoreFactory(tmp_path, default_client=BUILTIN_CLIENT)
    access = stores.access_configs()
    await access.add(AccessConfig(name="vert", access_type="vertical"))
    teachpoints = stores.teachpoints("arm_1")
    await teachpoints.add(_pad(AccessConfig(name="vert", access_type="vertical")))

    with pytest.raises(AccessConfigInUseError, match="arm_1.json"):
        await access.delete("vert")
    assert await access.get("vert") is not None
    await teachpoints.delete("pad_1")
    assert await access.delete("vert") is True


async def test_a_bad_teachpoint_entry_is_refused_naming_its_file(tmp_path: Path) -> None:
    access = AccessConfigFileStore(tmp_path / "access_configs.json")
    path = tmp_path / "arm_1.json"
    both = {"cartesian": {"x": 1, "y": 2, "z": 3, "yaw": 0, "pitch": 90, "roll": 180},
            "joints": {"base": 10.0}}
    path.write_text(json.dumps({"pad_1": both}), encoding="utf-8")

    with pytest.raises(SetupFileError, match="arm_1.json"):
        await TeachpointFileStore(path, tmp_path / "access_configs.json").get("pad_1")


async def test_keys_inside_an_entry_are_written_sorted(tmp_path: Path) -> None:
    access = AccessConfigFileStore(tmp_path / "access_configs.json")
    await access.add(AccessConfig(name="vert", access_type="vertical", vertical_clearance=20.0))

    text = (tmp_path / "access_configs.json").read_text(encoding="utf-8")
    entry_keys = list(json.loads(text)["vert"])
    assert entry_keys == sorted(entry_keys)


async def test_an_access_config_whose_key_differs_from_its_name_is_refused_naming_both(tmp_path: Path) -> None:
    (tmp_path / "access_configs.json").write_text(
        json.dumps({"renamed": {"name": "original", "access_type": "vertical"}}), encoding="utf-8",
    )

    with pytest.raises(SetupFileError, match="key 'renamed' has name 'original'; make them match") as refused:
        await PackageSetupStoreFactory(tmp_path, default_client=BUILTIN_CLIENT).access_configs().list()

    assert refused.value.path.name == "access_configs.json"


async def test_a_profile_whose_file_name_differs_from_its_name_is_refused_naming_both(tmp_path: Path) -> None:
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "renamed.json").write_text(
        json.dumps({"name": "original", "variables": {}}), encoding="utf-8",
    )

    with pytest.raises(SetupFileError, match="file name 'renamed' has name 'original'; make them match"):
        await PackageSetupStoreFactory(tmp_path, default_client=BUILTIN_CLIENT).profiles().list()
