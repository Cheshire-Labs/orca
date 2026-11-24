"""The setup stores of a deployment package, each one a file."""

import asyncio
import functools
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Generic, List, Optional, Tuple, TypeVar

from cheshire_drivers import DeckLayoutConfig
from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.labware_seed import load_labware_seed
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import (
    AccessConfig,
    CartesianCoordinates,
    JointCoordinates,
    Teachpoint,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typing_extensions import Self

from orca.runtime.access_config_store import AccessConfigInUseError
from orca.runtime.labware_catalog_store import (
    OPERATOR_CUSTOM_SOURCE,
    SEED_SOURCE,
    LabwareCatalogEntry,
    seed_entry_to_catalog_entry,
)
from orca.runtime.setup_files import (
    Entry,
    SetupFile,
    SetupEntryExistsError,
    SetupFileError,
    file_in,
    is_plain_file_name,
    read_setup_text,
    replace_file,
)
from orca.runtime.teachpoint_store import validate_persistable_access
from orca.variables.variable_definition import GlobalVariable


_logger = logging.getLogger(__name__)

Model = TypeVar("Model", bound=BaseModel)


def _model_file(
    path: Path, model: type[Model], name_of: Callable[[Model], str] | None = None,
) -> SetupFile[Model]:
    return SetupFile(
        path, model.model_validate, lambda e: e.model_dump(mode="json", exclude_none=True), name_of,
    )


class _KeyedFile(Generic[Entry]):
    """Add, update, replace and delete entries of one file, by key."""

    def __init__(self, file: SetupFile[Entry], what: str) -> None:
        self._file = file
        self._what = what

    async def get(self, key: str) -> Entry | None:
        return (await self._file.read()).get(key)

    async def all(self) -> dict[str, Entry]:
        return await self._file.read()

    async def add(self, key: str, entry: Entry) -> None:
        def _add(entries: dict[str, Entry]) -> dict[str, Entry]:
            if key in entries:
                raise SetupEntryExistsError(f"{self._what} {key!r} already registered")
            return {**entries, key: entry}
        await self._file.change(_add)

    async def update(self, key: str, entry: Entry) -> None:
        def _update(entries: dict[str, Entry]) -> dict[str, Entry]:
            if key not in entries:
                raise KeyError(f"{self._what} {key!r} not found")
            return {**entries, key: entry}
        await self._file.change(_update)

    async def put(self, key: str, entry: Entry) -> None:
        await self._file.change(lambda entries: {**entries, key: entry})

    async def delete(self, key: str) -> bool:
        removed = False

        def _delete(entries: dict[str, Entry]) -> dict[str, Entry]:
            nonlocal removed
            removed = key in entries
            return {k: v for k, v in entries.items() if k != key}
        await self._file.change(_delete)
        return removed


class _NoSchema:
    async def create_schema(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _access_config_file(path: Path) -> SetupFile[AccessConfig]:
    return _model_file(path, AccessConfig, lambda config: config.name)


class AccessConfigFileStore(_NoSchema):
    """``access_configs.json``. Deleting a config refuses while a teachpoint file names it;
    files have no foreign keys, so this reads the teachpoint files as a SQL store reads a table."""

    def __init__(self, path: Path) -> None:
        self._entries: _KeyedFile[AccessConfig] = _KeyedFile(
            _access_config_file(path), "AccessConfig",
        )
        self._teachpoints_dir = Path(path).parent / "teachpoints"

    async def get(self, name: str) -> AccessConfig | None:
        return await self._entries.get(name)

    async def list(self) -> List[AccessConfig]:
        entries = await self._entries.all()
        return [entries[key] for key in sorted(entries)]

    async def add(self, config: AccessConfig) -> None:
        await self._entries.add(config.name, config)

    async def update(self, config: AccessConfig) -> None:
        await self._entries.update(config.name, config)

    async def delete(self, name: str) -> bool:
        users = await self._teachpoint_files_naming(name)
        if users:
            raise AccessConfigInUseError(
                name, sum(count for _, count in users), tuple(file for file, _ in users),
            )
        return await self._entries.delete(name)

    async def _teachpoint_files_naming(self, name: str) -> List[Tuple[str, int]]:
        users = []
        for path in sorted(self._teachpoints_dir.rglob("*.json")):
            entries = await _model_file(path, TeachpointEntry).read()
            count = sum(1 for entry in entries.values() if entry.access == name)
            if count:
                users.append((path.relative_to(self._teachpoints_dir.parent).as_posix(), count))
        return users


def _patch_file(path: Path, what: str) -> _KeyedFile[MoveParameterPatch]:
    return _KeyedFile(_model_file(path, MoveParameterPatch), what)


class MoveDefaultsFileStore(_NoSchema):
    def __init__(self, path: Path) -> None:
        self._entries = _patch_file(path, "MoveDefaults")

    async def get(self, transporter_name: str) -> MoveParameterPatch | None:
        return await self._entries.get(transporter_name)

    async def list(self) -> dict[str, MoveParameterPatch]:
        return await self._entries.all()

    async def set(self, transporter_name: str, patch: MoveParameterPatch) -> None:
        await self._entries.put(transporter_name, patch)

    async def delete(self, transporter_name: str) -> bool:
        return await self._entries.delete(transporter_name)


class GripProfileFileStore(_NoSchema):
    def __init__(self, path: Path) -> None:
        self._entries = _patch_file(path, "GripProfile")

    async def get(self, labware_type: str) -> MoveParameterPatch | None:
        return await self._entries.get(labware_type)

    async def list(self) -> dict[str, MoveParameterPatch]:
        return await self._entries.all()

    async def set(self, labware_type: str, patch: MoveParameterPatch) -> None:
        await self._entries.put(labware_type, patch)

    async def delete(self, labware_type: str) -> bool:
        return await self._entries.delete(labware_type)


class GlobalVariableFileStore:
    """``global_variables.json``: each global's declaration and the value an operator set."""

    def __init__(self, path: Path) -> None:
        self._file: SetupFile[GlobalVariable] = SetupFile(
            path, GlobalVariable.model_validate, lambda entry: entry.model_dump(mode="json", exclude_unset=True),
        )

    async def read(self) -> dict[str, GlobalVariable]:
        return await self._file.read()

    async def write(self, entries: dict[str, GlobalVariable]) -> None:
        await self._file.write(entries)


class DeviceSetupFileStore:
    """``devices/<name>.json``, read as written. No file is a default sim; a setup naming no ``client`` goes to `default_client`."""

    def __init__(self, directory: Path, default_client: str) -> None:
        self._directory = Path(directory)
        self._default_client = default_client

    @property
    def default_client(self) -> str:
        return self._default_client

    def names(self) -> list[str]:
        return sorted(path.stem for path in self._directory.glob("*.json"))

    def read(self, name: str) -> DeviceSetup:
        path = file_in(self._directory, name, "Device")
        try:
            return DeviceSetup.model_validate_json(read_setup_text(path))
        except FileNotFoundError:
            return DeviceSetup()
        except ValidationError as refused:
            raise SetupFileError(path, str(refused)) from refused

    def write(self, name: str, setup: DeviceSetup) -> None:
        path = file_in(self._directory, name, "Device")
        fields = setup.model_dump(mode="json", exclude_defaults=True)
        # A fault's `kind` is its model's default and also what says which fault it is, so it is always written.
        for written, fault in zip(fields.get("sim", {}).get("faults", []), setup.sim.faults):
            written["kind"] = fault.kind
        text = json.dumps(fields, indent=2, sort_keys=True) + "\n"
        if DeviceSetup.model_validate_json(text) != setup:
            raise SetupFileError(path, "the setup would not read back as it was written, so it was not saved")
        replace_file(path, text)


class DeckLayoutFileStore(_NoSchema):
    def __init__(self, path: Path) -> None:
        self._entries: _KeyedFile[DeckLayoutConfig] = _KeyedFile(
            _model_file(path, DeckLayoutConfig), "DeckLayout",
        )

    async def get(self, name: str) -> DeckLayoutConfig | None:
        return await self._entries.get(name)

    async def list(self) -> List[Tuple[str, DeckLayoutConfig]]:
        return sorted((await self._entries.all()).items())

    async def add(self, name: str, config: DeckLayoutConfig) -> None:
        await self._entries.add(name, config)

    async def update(self, name: str, config: DeckLayoutConfig) -> None:
        await self._entries.update(name, config)

    async def delete(self, name: str) -> bool:
        return await self._entries.delete(name)


class TeachpointEntry(BaseModel):
    """A teachpoint as its file holds it. It names its access config rather than
    copying it, so an access-config edit reaches every teachpoint that uses it."""

    model_config = ConfigDict(extra="forbid")

    cartesian: Optional[CartesianCoordinates] = None
    joints: Optional[JointCoordinates] = None
    orientation: Optional[str] = None
    access: Optional[str] = None
    gateway: Optional[str] = None
    taught_with: Optional[str] = None
    by_labware: dict[str, MoveParameterPatch] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_kind_of_coordinates(self) -> Self:
        if self.cartesian is not None and self.joints is not None:
            raise ValueError("a teachpoint has cartesian or joints coordinates, not both")
        return self


def _entry_of(teachpoint: Teachpoint) -> TeachpointEntry:
    validate_persistable_access(teachpoint)
    coords = teachpoint.coordinates
    return TeachpointEntry(
        cartesian=coords if isinstance(coords, CartesianCoordinates) else None,
        joints=coords if isinstance(coords, JointCoordinates) else None,
        orientation=teachpoint.orientation,
        access=teachpoint.access_config_name,
        gateway=teachpoint.gateway,
        taught_with=teachpoint.taught_with,
        by_labware=dict(teachpoint.by_labware),
    )


class TeachpointFileStore(_NoSchema):
    """One transporter's ``teachpoints/<name>.json``. Each entry names an access config,
    which reads fill in from ``access_configs.json``, as a SQL store joins a table."""

    def __init__(self, path: Path, access_configs_path: Path) -> None:
        self._path = Path(path)
        self._entries: _KeyedFile[TeachpointEntry] = _KeyedFile(
            _model_file(path, TeachpointEntry), "Teachpoint",
        )
        self._access_configs = _access_config_file(access_configs_path)

    def _teachpoint(
        self, position_id: str, entry: TeachpointEntry, access_configs: dict[str, AccessConfig],
    ) -> Teachpoint:
        access = None
        if entry.access is not None:
            access = access_configs.get(entry.access)
            if access is None:
                raise SetupFileError(
                    self._path,
                    f"teachpoint {position_id!r} names access config {entry.access!r}, "
                    "which access_configs.json does not have",
                )
        try:
            return Teachpoint(
                position_id=position_id,
                coordinates=entry.cartesian if entry.cartesian is not None else entry.joints,
                orientation=entry.orientation, access=access, gateway=entry.gateway,
                taught_with=entry.taught_with, by_labware=entry.by_labware,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise SetupFileError(self._path, f"teachpoint {position_id!r}: {exc}") from exc

    async def get(self, position_id: str) -> Teachpoint | None:
        entry = await self._entries.get(position_id)
        if entry is None:
            return None
        return self._teachpoint(position_id, entry, await self._access_configs.read())

    async def resolve(self, position_id: str) -> Teachpoint | None:
        return await self.get(position_id)

    async def list(self) -> List[Teachpoint]:
        entries = await self._entries.all()
        access_configs = await self._access_configs.read()
        return [self._teachpoint(key, entries[key], access_configs) for key in sorted(entries)]

    async def _checked_entry(self, teachpoint: Teachpoint) -> TeachpointEntry:
        entry = _entry_of(teachpoint)
        if entry.access is not None and entry.access not in await self._access_configs.read():
            raise KeyError(
                f"Teachpoint {teachpoint.position_id!r} names access config {entry.access!r}, "
                "which access_configs.json does not have; add the access config first",
            )
        return entry

    async def add(self, teachpoint: Teachpoint) -> None:
        await self._entries.add(teachpoint.position_id, await self._checked_entry(teachpoint))

    async def update(self, teachpoint: Teachpoint) -> None:
        await self._entries.update(teachpoint.position_id, await self._checked_entry(teachpoint))

    async def delete(self, position_id: str) -> bool:
        return await self._entries.delete(position_id)


@functools.cache
def _built_in_labware() -> dict[str, LabwareCatalogEntry]:
    return {e.labware_type: seed_entry_to_catalog_entry(e) for e in load_labware_seed()}


class LabwareCatalogFileStore(_NoSchema):
    """Built-in labware from cheshire-drivers, plus one ``<dir>/<labware_type>.json``
    per labware the deployment added. Built-ins are never written here."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    def _path_for(self, labware_type: str) -> Path:
        return file_in(self._dir, labware_type, "labware_type")

    @staticmethod
    def _has_a_built_in_name(path: Path) -> bool:
        # A cheshire-drivers upgrade can add a built-in under a name a deployment already used.
        if path.stem not in _built_in_labware():
            return False
        _logger.warning("Ignoring %s: %r is a built-in labware name; rename the file and the labware_type inside it", path, path.stem)
        return True

    def _load(self, path: Path) -> LabwareCatalogEntry:
        try:
            raw = json.loads(read_setup_text(path))
            entry = LabwareCatalogEntry.model_validate(
                {**raw, "source": OPERATOR_CUSTOM_SOURCE},
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
            raise SetupFileError(path, str(exc)) from exc
        if entry.labware_type != path.stem:
            raise SetupFileError(
                path, f"holds labware_type {entry.labware_type!r}; the file name must match",
            )
        return entry

    def _added_now(self) -> list[LabwareCatalogEntry]:
        if not self._dir.is_dir():
            return []
        return [
            self._load(path) for path in self._dir.iterdir()
            if path.is_file() and path.suffix == ".json" and not self._has_a_built_in_name(path)
        ]

    def _get_now(self, labware_type: str) -> LabwareCatalogEntry | None:
        if not is_plain_file_name(labware_type):
            return None
        path = self._path_for(labware_type)
        if path.is_file() and not self._has_a_built_in_name(path):
            return self._load(path)
        return _built_in_labware().get(labware_type)

    async def list(self, category: str | None = None) -> list[LabwareCatalogEntry]:
        added = await asyncio.to_thread(self._added_now)
        entries = [*_built_in_labware().values(), *added]
        return sorted(
            (e for e in entries if category is None or e.category == category),
            key=lambda e: e.labware_type,
        )

    async def get(self, labware_type: str) -> LabwareCatalogEntry | None:
        return await asyncio.to_thread(self._get_now, labware_type)

    async def put(self, entry: LabwareCatalogEntry) -> None:
        if entry.source == SEED_SOURCE or entry.labware_type in _built_in_labware():
            raise ValueError(f"{entry.labware_type!r} is built in; it is never written to a file")
        path = self._path_for(entry.labware_type)
        text = json.dumps(
            entry.model_dump(mode="json", exclude={"source"}), indent=2, sort_keys=True,
        ) + "\n"
        await asyncio.to_thread(replace_file, path, text)

    async def delete(self, labware_type: str) -> None:
        if not is_plain_file_name(labware_type):
            return
        path = self._path_for(labware_type)
        await asyncio.to_thread(path.unlink, True)
