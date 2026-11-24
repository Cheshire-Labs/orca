"""Deployment profiles are `profiles/<name>.json` files, read on every call."""

import json
from pathlib import Path

import pytest

from orca.runtime.profile_store import FileDeploymentProfileStore
from orca.runtime.setup_files import SetupFileError
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

    async def test_a_hand_edit_is_read_on_the_next_call(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path)
        await store.add(_profile("p"))
        (tmp_path / "p.json").write_text(
            _profile("p", variables={"speed": 42}).model_dump_json(),
        )
        loaded = await store.get("p")
        assert loaded is not None
        assert loaded.variables == {"speed": 42}
        (tmp_path / "added_by_hand.json").write_text(
            _profile("added_by_hand").model_dump_json(),
        )
        assert sorted(p.name for p in await store.list()) == ["added_by_hand", "p"]

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

    async def test_a_missing_folder_reads_as_no_profiles(self, tmp_path: Path) -> None:
        store = FileDeploymentProfileStore(tmp_path / "fresh")
        assert await store.list() == []
        await store.add(_profile("p"))
        assert (tmp_path / "fresh" / "p.json").exists()

    async def test_invalid_json_is_refused_naming_the_file(self, tmp_path: Path) -> None:
        (tmp_path / "bad.json").write_text("{not valid json")
        with pytest.raises(SetupFileError, match="bad.json"):
            await FileDeploymentProfileStore(tmp_path).list()

    async def test_extra_field_in_json_rejected(
        self, tmp_path: Path,
    ) -> None:
        # extra="forbid" must fire on the file load path too.
        (tmp_path / "extra.json").write_text(
            json.dumps({"name": "extra", "variables": {}, "bogus": 1}),
        )
        with pytest.raises(SetupFileError, match="extra.json"):
            await FileDeploymentProfileStore(tmp_path).get("extra")

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


async def test_a_profile_name_that_is_not_a_plain_file_name_is_refused(tmp_path: Path) -> None:
    store = FileDeploymentProfileStore(tmp_path / "profiles")
    (tmp_path / "access_configs.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="file name"):
        await store.add(_profile("../access_configs"))
    with pytest.raises(ValueError, match="file name"):
        await store.delete("../access_configs")
    assert (tmp_path / "access_configs.json").exists()
