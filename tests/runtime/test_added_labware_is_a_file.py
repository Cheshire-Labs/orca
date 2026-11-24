"""Built-in labware comes from cheshire-drivers; labware you add is `labware/<name>.json`."""

import json
from pathlib import Path

import pytest
from cheshire_drivers.labware_seed import load_labware_seed

from orca.runtime.labware_catalog_service import (
    LabwareCatalogConflict,
    LabwareCatalogService,
    LabwareGeometryInvalid,
    SeedLabwareReadOnly,
)
from orca.runtime.labware_catalog_store import (
    OPERATOR_CUSTOM_SOURCE,
    SEED_SOURCE,
    LabwareCatalogEntry,
    seed_entry_to_catalog_entry,
)
from orca.runtime.setup_file_stores import LabwareCatalogFileStore
from orca.runtime.setup_files import SetupFileError


def _builtin() -> LabwareCatalogEntry:
    plate = next(e for e in load_labware_seed() if e.category == "plate")
    return seed_entry_to_catalog_entry(plate)


def _added(labware_type: str) -> LabwareCatalogEntry:
    builtin = _builtin()
    geometry = dict(builtin.geometry)
    geometry["labware_type"] = labware_type
    return builtin.model_copy(update={
        "labware_type": labware_type, "display_name": "Our plate",
        "source": OPERATOR_CUSTOM_SOURCE, "geometry": geometry,
    })


def _service(root: Path) -> LabwareCatalogService:
    return LabwareCatalogService(LabwareCatalogFileStore(root / "labware"))


async def test_the_built_ins_are_read_only(tmp_path: Path) -> None:
    service = _service(tmp_path)
    builtin = _builtin()

    assert (await service.get(builtin.labware_type)).source == SEED_SOURCE
    with pytest.raises(SeedLabwareReadOnly):
        await service.delete(builtin.labware_type)
    assert not (tmp_path / "labware").exists()


async def test_added_labware_is_a_file_a_new_process_reads(tmp_path: Path) -> None:
    await _service(tmp_path).add(_added("our_plate"))

    on_disk = json.loads((tmp_path / "labware" / "our_plate.json").read_text(encoding="utf-8"))
    assert on_disk["display_name"] == "Our plate"
    assert "source" not in on_disk
    read = await _service(tmp_path).get("our_plate")
    assert read.source == OPERATOR_CUSTOM_SOURCE
    assert read.display_name == "Our plate"


async def test_a_hand_edit_is_read_on_the_next_call(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await service.add(_added("our_plate"))
    path = tmp_path / "labware" / "our_plate.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["display_name"] = "Renamed by hand"
    path.write_text(json.dumps(on_disk), encoding="utf-8")

    assert (await service.get("our_plate")).display_name == "Renamed by hand"


def _write_file_with_the_name_of(tmp_path: Path, taken: str) -> Path:
    path = tmp_path / "labware" / f"{taken}.json"
    path.parent.mkdir()
    path.write_text(_added(taken).model_dump_json(exclude={"source"}), encoding="utf-8")
    return path


async def test_adding_a_name_a_built_in_uses_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LabwareCatalogConflict):
        await _service(tmp_path).add(_added(_builtin().labware_type))


async def test_a_file_with_a_built_in_name_is_skipped_by_list_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    taken = _builtin().labware_type
    path = _write_file_with_the_name_of(tmp_path, taken)

    entries = [e for e in await _service(tmp_path).list() if e.labware_type == taken]

    assert [e.source for e in entries] == [SEED_SOURCE]
    assert str(path) in caplog.text
    assert path.exists()


async def test_list_holds_built_ins_and_added_labware(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await service.add(_added("our_plate"))

    types = [e.labware_type for e in await service.list("plate")]
    assert "our_plate" in types
    assert _builtin().labware_type in types
    assert types == sorted(types)
    not_a_plate = next(e.labware_type for e in load_labware_seed() if e.category != "plate")
    assert not_a_plate not in types


async def test_a_labware_type_that_is_not_a_plain_file_name_is_invalid(tmp_path: Path) -> None:
    with pytest.raises(LabwareGeometryInvalid, match="file name"):
        await _service(tmp_path).add(_added("plates/ours"))


async def test_get_of_a_built_in_answers_the_built_in_past_a_file_with_its_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    taken = _builtin().labware_type
    path = _write_file_with_the_name_of(tmp_path, taken)

    assert (await _service(tmp_path).get(taken)).source == SEED_SOURCE
    assert str(path) in caplog.text


async def test_update_and_delete_of_that_name_stay_read_only_and_keep_the_file(tmp_path: Path) -> None:
    taken = _builtin().labware_type
    path = _write_file_with_the_name_of(tmp_path, taken)
    service = _service(tmp_path)

    with pytest.raises(SeedLabwareReadOnly):
        await service.update(_added(taken))
    with pytest.raises(SeedLabwareReadOnly):
        await service.delete(taken)
    assert path.exists()


async def test_a_broken_added_file_is_still_refused(tmp_path: Path) -> None:
    (tmp_path / "labware").mkdir()
    (tmp_path / "labware" / "our_plate.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(SetupFileError, match="our_plate"):
        await _service(tmp_path).list()
