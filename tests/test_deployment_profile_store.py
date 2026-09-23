"""Tests for IDeploymentProfileStore impls (Null + FileDeploymentProfileStore).

Mirrors test_access_config_store / test_teachpoint_store. The source-available store is
file-backed (directory-of-JSONs) because deployment profiles are operator
artefacts that should survive process restarts even without the hosted deployment DB.
"""

import json
from pathlib import Path

import pytest

from orca.runtime.profile_store import (
    FileDeploymentProfileStore,
    NullDeploymentProfileStore,
)
from orca.variables.deployment_profile import DeploymentProfile


def _profile(name: str = "p", **overrides: object) -> DeploymentProfile:
    base = {
        "name": name,
        "description": "test profile",
        "variables": {"speed": 500, "global.retries": 3},
        "computed": {"derived": "speed * 2"},
    }
    base.update(overrides)
    return DeploymentProfile.model_validate(base)


class TestNullDeploymentProfileStore:
    """The no-op default. Methods complete without doing anything observable."""

    async def test_get_returns_none(self) -> None:
        assert await NullDeploymentProfileStore().get("anything") is None

    async def test_list_returns_empty(self) -> None:
        assert await NullDeploymentProfileStore().list() == []

    async def test_add_is_noop(self) -> None:
        store = NullDeploymentProfileStore()
        await store.add(_profile("p1"))
        assert await store.get("p1") is None
        assert await store.list() == []

    async def test_update_is_noop(self) -> None:
        store = NullDeploymentProfileStore()
        await store.update(_profile("p1"))
        assert await store.get("p1") is None
        assert await store.list() == []

    async def test_delete_returns_false(self) -> None:
        assert await NullDeploymentProfileStore().delete("anything") is False


class TestFileDeploymentProfileStore:

    async def test_get_returns_none_for_unknown(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        assert await store.get("missing") is None

    async def test_add_then_get_roundtrips(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        p = _profile("p1")
        await store.add(p)
        loaded = await store.get("p1")
        assert loaded is not None
        assert loaded.name == "p1"
        assert loaded.variables == {"speed": 500, "global.retries": 3}
        assert loaded.computed == {"derived": "speed * 2"}

    async def test_add_writes_file_through(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        await store.add(_profile("on_disk"))
        f = tmp_path / "on_disk.json"
        assert f.exists()
        on_disk = json.loads(f.read_text())
        assert on_disk["name"] == "on_disk"
        assert on_disk["variables"] == {"speed": 500, "global.retries": 3}

    async def test_list_returns_all(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        await store.add(_profile("a"))
        await store.add(_profile("b"))
        await store.add(_profile("c"))
        names = sorted(p.name for p in await store.list())
        assert names == ["a", "b", "c"]

    async def test_loads_existing_dir_on_construction(
        self, tmp_path: Path,
    ) -> None:
        # Pre-populate the dir, then construct: should pick up existing files.
        (tmp_path / "preexisting.json").write_text(
            _profile("preexisting").model_dump_json(),
        )
        store = FileDeploymentProfileStore(tmp_path)
        loaded = await store.get("preexisting")
        assert loaded is not None
        assert loaded.name == "preexisting"

    async def test_add_duplicate_name_raises(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        await store.add(_profile("dup"))
        with pytest.raises(ValueError, match="already registered"):
            await store.add(_profile("dup"))

    async def test_update_replaces_value(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        await store.add(_profile("p"))
        replacement = _profile("p", variables={"speed": 9999})
        await store.update(replacement)
        loaded = await store.get("p")
        assert loaded is not None
        assert loaded.variables == {"speed": 9999}

    async def test_update_writes_file_through(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        await store.add(_profile("p"))
        await store.update(_profile("p", description="updated"))
        on_disk = json.loads((tmp_path / "p.json").read_text())
        assert on_disk["description"] == "updated"

    async def test_update_unknown_raises(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        with pytest.raises(KeyError, match="not found"):
            await store.update(_profile("ghost"))

    async def test_delete_returns_true_when_present(
        self, tmp_path: Path,
    ) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        await store.add(_profile("p"))
        assert await store.delete("p") is True
        assert await store.get("p") is None
        assert not (tmp_path / "p.json").exists()

    async def test_delete_returns_false_when_absent(
        self, tmp_path: Path,
    ) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        assert await store.delete("ghost") is False

    async def test_construction_creates_dir_if_missing(
        self, tmp_path: Path,
    ) -> None:
        target = tmp_path / "fresh"
        assert not target.exists()
        FileDeploymentProfileStore(target)
        assert target.is_dir()

    async def test_invalid_json_in_dir_raises_at_construction(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "bad.json").write_text("{not valid json")
        with pytest.raises((ValueError, json.JSONDecodeError)):
            FileDeploymentProfileStore(tmp_path)

    async def test_extra_field_in_json_rejected(
        self, tmp_path: Path,
    ) -> None:
        # extra="forbid" must fire on the file load path too.
        (tmp_path / "extra.json").write_text(
            json.dumps({"name": "extra", "variables": {}, "bogus": 1}),
        )
        with pytest.raises(ValueError):
            FileDeploymentProfileStore(tmp_path)

    async def test_add_with_mismatched_filename_uses_profile_name(
        self, tmp_path: Path,
    ) -> None:
        # Whatever the on-disk filename history is, add(profile) keys by
        # profile.name and writes <profile.name>.json.
        store = FileDeploymentProfileStore(tmp_path)
        await store.add(_profile("canonical"))
        assert (tmp_path / "canonical.json").exists()

    async def test_directory_only_loads_dot_json_files(
        self, tmp_path: Path,
    ) -> None:
        # Operators may put README.md or backups in the dir. We only consider
        # *.json on load.
        (tmp_path / "p.json").write_text(_profile("p").model_dump_json())
        (tmp_path / "README.md").write_text("not a profile")
        (tmp_path / "p.json.bak").write_text("backup file should be ignored")
        store = FileDeploymentProfileStore(tmp_path)
        names = [p.name for p in await store.list()]
        assert names == ["p"]
