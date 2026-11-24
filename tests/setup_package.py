"""Write a test's setup into its temp deployment package, the way an operator would.

Setup is files. These helpers write them before a topology reads them,
and return the store the device reads through.
"""

import json
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TypeVar

from cheshire_drivers import DeckLayoutConfig
from cheshire_drivers.teachpoints import AccessConfig, Teachpoint
from pydantic import JsonValue

from orca.devices.devices import LiquidHandler
from orca.resource_models.transporter import Transporter
from orca.runtime.deck_layout_service import DeckLayoutService
from orca.resource_models.store_interfaces import IDeckLayoutStore, ITeachpointStore
from orca.runtime.setup_file_stores import (
    AccessConfigFileStore,
    GripProfileFileStore,
    MoveDefaultsFileStore,
    TeachpointFileStore,
    _entry_of,
)
from orca.runtime.store_factory import default_package_root
from orca.runtime.startup import PackageSetupStoreFactory
from orca.runtime.teachpoint_service import TeachpointService
from cheshire_drivers.device_setup import BUILTIN_CLIENT


def package_stores(root: Path | None = None) -> PackageSetupStoreFactory:
    return PackageSetupStoreFactory(root or default_package_root(), default_client=BUILTIN_CLIENT)


def _write(path: Path, entries: Mapping[str, JsonValue], *, merge: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(path.read_text(encoding="utf-8")) if merge and path.exists() else {}
    current.update(entries)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_access_configs(
    stores: PackageSetupStoreFactory, configs: Iterable[AccessConfig],
) -> None:
    _write(
        stores.root / "access_configs.json",
        {c.name: c.model_dump(mode="json") for c in configs},
        merge=True,
    )


def write_teachpoints(
    stores: PackageSetupStoreFactory, device: str, teachpoints: Iterable[Teachpoint],
) -> TeachpointService:
    teachpoints = list(teachpoints)
    write_access_configs(stores, [
        AccessConfig(
            name=tp.access_config_name,
            access_type="horizontal" if tp.access_type == "horizontal" else "vertical",
            gripper_offset=tp.gripper_offset,
            vertical_clearance=tp.vertical_clearance,
            horizontal_clearance=tp.horizontal_clearance,
        )
        for tp in teachpoints if tp.access_config_name is not None
    ])
    _write(
        stores.root / "teachpoints" / f"{device}.json",
        {tp.position_id: _entry_of(tp).model_dump(mode="json", exclude_none=True) for tp in teachpoints},
        merge=False,
    )
    return stores.teachpoints(device)


def write_deck_layouts(
    stores: PackageSetupStoreFactory, device: str, layouts: Mapping[str, DeckLayoutConfig],
) -> DeckLayoutService:
    _write(
        stores.root / "deck_layouts" / f"{device}.json",
        {name: config.model_dump(mode="json", exclude_none=True) for name, config in layouts.items()},
        merge=False,
    )
    return stores.deck_layouts(device)


def _fresh_package() -> PackageSetupStoreFactory:
    return package_stores(fresh_root())


def teachpoint_file(
    teachpoints: Iterable[Teachpoint] = (), access_configs: Iterable[AccessConfig] = (),
) -> TeachpointService:
    """One device's teachpoints in a package of their own."""
    stores = _fresh_package()
    write_access_configs(stores, access_configs)
    return write_teachpoints(stores, "device", teachpoints)


def deck_layout_file(layouts: Mapping[str, DeckLayoutConfig] | None = None) -> DeckLayoutService:
    """One device's deck layouts in a package of their own."""
    return write_deck_layouts(_fresh_package(), "device", layouts or {})


_T = TypeVar("_T", bound=Transporter)
_LH = TypeVar("_LH", bound=LiquidHandler)


def with_teachpoints(transporter: _T, store: ITeachpointStore) -> _T:
    """A transporter used without a build, which is what attaches its file."""
    transporter.bind_teachpoints(store)
    return transporter


def with_deck_layouts(handler: _LH, store: IDeckLayoutStore) -> _LH:
    """A liquid handler used without a build, which is what attaches its file."""
    handler.bind_deck_layouts(store)
    return handler


def fresh_root() -> Path:
    """A package folder no other test writes to."""
    return default_package_root() / f"pkg-{uuid.uuid4().hex[:8]}"


def fresh_access_configs() -> AccessConfigFileStore:
    return AccessConfigFileStore(fresh_root() / "access_configs.json")


def fresh_move_defaults() -> MoveDefaultsFileStore:
    return MoveDefaultsFileStore(fresh_root() / "move_defaults.json")


def fresh_grip_profiles() -> GripProfileFileStore:
    return GripProfileFileStore(fresh_root() / "grip_profiles.json")


def fresh_teachpoints(access_configs: Iterable[AccessConfig] = ()) -> TeachpointFileStore:
    """One device's teachpoint file, with the access configs it may name."""
    stores = package_stores(fresh_root())
    write_access_configs(stores, access_configs)
    return TeachpointFileStore(
        stores.root / "teachpoints" / "device.json", stores.root / "access_configs.json",
    )
