"""A setup file is read on every call, written whole or not at all, and refused when malformed."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from cheshire_drivers.teachpoints import AccessConfig

from orca.runtime.access_config_service import AccessConfigService
from orca.runtime.setup_file_stores import AccessConfigFileStore, _model_file
from orca.runtime.setup_files import SetupFile, SetupFileError, replace_file


class _Entry(BaseModel):
    value: int


def _file(path: Path) -> SetupFile[_Entry]:
    return SetupFile(path, _Entry.model_validate, lambda e: e.model_dump(mode="json"))


async def test_a_missing_file_reads_as_no_entries(tmp_path: Path) -> None:
    assert await _file(tmp_path / "x.json").read() == {}


async def test_a_write_is_read_back_and_is_stable_sorted_json(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "x.json"
    await _file(path).write({"b": _Entry(value=2), "a": _Entry(value=1)})

    assert await _file(path).read() == {"a": _Entry(value=1), "b": _Entry(value=2)}
    assert path.read_text(encoding="utf-8") == (
        '{\n  "a": {\n    "value": 1\n  },\n  "b": {\n    "value": 2\n  }\n}\n'
    )


async def test_an_edit_by_hand_is_seen_on_the_next_read(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    store = _file(path)
    await store.write({"a": _Entry(value=1)})

    path.write_text(json.dumps({"a": {"value": 5}}), encoding="utf-8")

    assert await store.read() == {"a": _Entry(value=5)}


async def test_a_malformed_file_is_refused_with_its_path(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text('{"a": {"value": "not a number"}}', encoding="utf-8")

    with pytest.raises(SetupFileError) as refused:
        await _file(path).read()

    assert str(path) in str(refused.value)
    assert "a" in str(refused.value)


async def test_a_file_that_is_not_json_is_refused_with_its_path(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SetupFileError, match="x.json"):
        await _file(path).read()


async def test_a_failed_write_leaves_the_old_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "x.json"
    store = _file(path)
    await store.write({"a": _Entry(value=1)})
    before = path.read_text(encoding="utf-8")

    def _crash(src: str, dst: str) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", _crash)
    with pytest.raises(OSError):
        await store.write({"a": _Entry(value=2)})

    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


async def test_concurrent_changes_through_one_service_all_land(tmp_path: Path) -> None:
    service = AccessConfigService(AccessConfigFileStore(tmp_path / "access_configs.json"))

    await asyncio.gather(*(
        service.add(AccessConfig(name=f"k{i}", access_type="vertical")) for i in range(20)
    ))

    assert {config.name for config in await service.list()} == {f"k{i}" for i in range(20)}


def test_a_rename_blocked_by_another_handle_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_replace = os.replace
    blocked = [PermissionError(), PermissionError()]

    def replace_after_two_refusals(source: str, target: Path) -> None:
        if blocked:
            raise blocked.pop()
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", replace_after_two_refusals)
    path = tmp_path / "access_configs.json"
    replace_file(path, "{}\n")
    assert path.read_text(encoding="utf-8") == "{}\n"
    assert blocked == []


async def test_a_file_that_is_not_utf8_names_its_path(tmp_path: Path) -> None:
    path = tmp_path / "access_configs.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(SetupFileError, match="access_configs.json"):
        await _model_file(path, AccessConfig).read()
