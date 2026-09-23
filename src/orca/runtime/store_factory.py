"""Per-deployment factory that produces the runtime's calibration stores.

Topology code asks the factory for a per-device store at build time:

    robot1 = Transporter(
        "robot1",
        SimTransporterDriver("robot1"),
        teachpoint_store=stores.teachpoints("robot1", seed=[Teachpoint(...)]),
    )

The factory implementation decides the storage substrate: standalone local orca
uses `InMemoryRuntimeStoreFactory`; a hosted deployment ships a DB-backed factory.

Stores returned by the factory are pure CRUD per their Protocol. The
factory's job is construction and impl selection; nothing else.
"""

from pathlib import Path
from typing import List, Mapping, Optional, Protocol

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from cheshire_drivers.teachpoints import AccessConfig, Teachpoint

from orca.runtime.access_config_service import AccessConfigService
from orca.runtime.deck_layout_service import seeded_deck_layout_service
from orca.runtime.db import create_memory_engine
from orca.runtime.sqlite_access_config_store import SqliteAccessConfigStore
from orca.runtime.move_defaults_service import MoveDefaultsService
from orca.runtime.sqlite_move_defaults_store import SqliteMoveDefaultsStore
from orca.runtime.grip_profile_service import GripProfileService
from orca.runtime.sqlite_grip_profile_store import SqliteGripProfileStore
from orca.runtime.interfaces import (
    IAccessConfigStore,
    IGripProfileStore,
    IDeckLayoutStore,
    IDeploymentProfileStore,
    ITeachpointStore,
)
from orca.runtime.labware_catalog import StoreBackedLabwareCatalog
from orca.runtime.labware_catalog_protocol import ILabwareCatalog
from orca.runtime.labware_catalog_service import (
    LabwareCatalogService,
    seeded_labware_catalog_service,
)
from orca.runtime.profile_store import FileDeploymentProfileStore
from orca.runtime.teachpoint_service import seeded_teachpoint_service
from orca.variables.variable_store import VariableService, VariableStore


class IRuntimeStoreFactory(Protocol):
    """Builds per-device calibration stores for a single deployment.

    `teachpoints` / `deck_layouts` are per-device; `access_configs` is a
    deployment-wide singleton. Each method returns a store instance that
    the device constructor accepts directly.

    Seed semantics are uniform across impls: `teachpoints`, `deck_layouts`,
    and `access_configs` all accept an optional build-time seed snapshot.
    The async builder reads it at build time so the System graph resolves
    topology-declared values, and `apply_seeds` reconciles it into the
    substrate (insert-if-missing; existing rows, including operator edits via
    REST/MCP, are never overwritten). In-memory realizes the seed lazily on
    first async access; DB-backed reconciles it into the database.
    """

    def teachpoints(
        self,
        device_id: str,
        seed: Optional[List[Teachpoint]] = None,
    ) -> ITeachpointStore: ...

    def deck_layouts(
        self,
        device_id: str,
        seed: Optional[Mapping[str, DeckLayoutConfig]] = None,
    ) -> IDeckLayoutStore: ...

    def access_configs(
        self,
        seed: Optional[List[AccessConfig]] = None,
    ) -> IAccessConfigStore: ...

    def move_defaults(self) -> MoveDefaultsService: ...

    def grip_profiles(self) -> IGripProfileStore: ...

    def profiles(self) -> IDeploymentProfileStore: ...

    def variable_store(self) -> VariableService:
        """Return a fresh, per-build variable store (in-memory, not persisted).

        One per System build: variables are runtime state, not a deployment-wide
        registry, so unlike teachpoints/access-configs there is no shared singleton.
        """
        ...

    def labware_catalog_store(self) -> LabwareCatalogService:
        """Return the deployment's MUTABLE labware-catalog registry.

        The single source of truth for catalog rows (seed + operator-custom):
        ONE Service (lock + policy) shared by the runtime (injected as
        ``labware_catalog_store``), the deployment-registries operator surface,
        and the read-only ``labware_catalog()`` view below, which reads through
        this Service per query.
        """
        ...

    def labware_catalog(self) -> ILabwareCatalog:
        """Return the deployment's labware definition catalog.

        Reads the mutable ``labware_catalog_store`` per query: no
        build-time snapshot, so operator edits resolve on the next read. Source-available
        default wraps the in-memory store; a hosted deployment wraps its DB-backed store.

        Sync: constructing the store-backed view needs no I/O; the awaited
        reads happen at query time, not here.
        """
        ...

    async def preload_for_build(self) -> None:
        """One-shot async hook the runtime lifecycle calls before `build()`.

        Implementations whose stores need async setup (DB-backed catalog
        snapshot, network warm-up) do that work here; impls that are
        already sync-ready treat this as a no-op. Called exactly once per
        build, before `deployment_package.system:build(stores)` runs.
        """
        ...

    async def apply_seeds(self) -> None:
        """Reconcile topology-declared seeds with the underlying substrate.

        Called once by the runtime after topology construction and before
        `runtime.start`. Implementations whose stores already realize their
        seed at construction (e.g. in-memory) treat this as a no-op. DB-backed
        implementations use it to idempotently insert seeded rows that have
        not yet been written, so async lookups (`store.resolve(...)`) can see
        topology-declared values without an operator first re-uploading them.
        Existing rows are never overwritten -- operator edits via REST/MCP win.
        """
        ...


class InMemoryRuntimeStoreFactory:
    """Source-available default factory: every store is in-process state.

    The access-config and deployment-profile stores are deployment-wide
    singletons constructed on first call. Subsequent calls return the
    same instance.

    `profiles()` is file-backed (directory-of-JSONs) instead of pure
    in-memory because deployment profiles are operator artefacts that
    should survive process restarts. The directory defaults to
    ``./profiles`` relative to CWD; callers that want a custom path
    construct ``InMemoryRuntimeStoreFactory(profiles_dir=...)``.
    """

    def __init__(
        self,
        profiles_dir: Optional[Path] = None,
        catalog_service: Optional[LabwareCatalogService] = None,
    ) -> None:
        self._access_configs: Optional[AccessConfigService] = None
        self._seeded_access_configs: List[AccessConfig] = []
        self._move_defaults: Optional[MoveDefaultsService] = None
        self._grip_profiles: Optional[GripProfileService] = None
        self._profiles_dir = (
            Path(profiles_dir) if profiles_dir is not None
            else Path("profiles")
        )
        self._profiles: Optional[IDeploymentProfileStore] = None
        # Injectable so the daemon shares ONE deployment-wide Service across
        # mount/unload cycles + the runtime + the deployment-registries layer.
        self._catalog_service: LabwareCatalogService = (
            catalog_service
            if catalog_service is not None
            else seeded_labware_catalog_service()
        )
        self._owns_catalog = catalog_service is None

    def teachpoints(
        self,
        device_id: str,
        seed: Optional[List[Teachpoint]] = None,
    ) -> ITeachpointStore:
        del device_id
        # Lazy-seeded so the build-time reachability read (store.list()) sees the
        # seed on first async access, before apply_seeds() runs.
        return seeded_teachpoint_service(seed or [])

    def deck_layouts(
        self,
        device_id: str,
        seed: Optional[Mapping[str, DeckLayoutConfig]] = None,
    ) -> IDeckLayoutStore:
        del device_id
        # Lazy-seeded so the build-time deck read (store.get/list) sees the
        # seed on first async access, before apply_seeds() runs.
        return seeded_deck_layout_service(seed or {})

    def access_configs(
        self,
        seed: Optional[List[AccessConfig]] = None,
    ) -> AccessConfigService:
        # Seeds accumulate here; the async insert-if-missing runs in apply_seeds()
        # because the DB write cannot happen at this sync construction call.
        if self._access_configs is None:
            self._access_configs = AccessConfigService(
                SqliteAccessConfigStore(create_memory_engine())
            )
        if seed is not None:
            self._seeded_access_configs.extend(seed)
        return self._access_configs

    def move_defaults(self) -> MoveDefaultsService:
        if self._move_defaults is None:
            self._move_defaults = MoveDefaultsService(
                SqliteMoveDefaultsStore(create_memory_engine())
            )
        return self._move_defaults

    def grip_profiles(self) -> GripProfileService:
        if self._grip_profiles is None:
            self._grip_profiles = GripProfileService(
                SqliteGripProfileStore(create_memory_engine())
            )
        return self._grip_profiles

    def profiles(self) -> IDeploymentProfileStore:
        if self._profiles is None:
            self._profiles = FileDeploymentProfileStore(self._profiles_dir)
        return self._profiles

    def variable_store(self) -> VariableService:
        return VariableService(VariableStore())

    def labware_catalog_store(self) -> LabwareCatalogService:
        return self._catalog_service

    def labware_catalog(self) -> ILabwareCatalog:
        # The read view reads through the Service per query; no build-time
        # snapshot sits in front of it, so operator edits resolve on the next read.
        return StoreBackedLabwareCatalog(self._catalog_service)

    async def preload_for_build(self) -> None:
        """No-op: in-memory factory loads everything synchronously on demand."""
        return None

    async def apply_seeds(self) -> None:
        """Reconcile accumulated access-config seeds into SQLite.

        Insert-if-missing so a second mount preserves operator edits. The
        access-config Service writes through an async DB, so its seed is
        realized here, not at the sync construction call. Per-device teachpoint
        and deck-layout services are lazy-seeded and reconcile their seed on
        first async access, so they need no work here.
        """
        if self._access_configs is not None and self._seeded_access_configs:
            await self._access_configs.seed_if_missing(self._seeded_access_configs)

    async def aclose(self) -> None:
        """Dispose the daemon-lifetime engines this factory owns. Per-device
        teachpoint/deck stores are the runtime's to close at its shutdown."""
        if self._access_configs is not None:
            await self._access_configs.aclose()
        if self._move_defaults is not None:
            await self._move_defaults.aclose()
        if self._grip_profiles is not None:
            await self._grip_profiles.aclose()
        if self._owns_catalog:
            await self._catalog_service.aclose()
