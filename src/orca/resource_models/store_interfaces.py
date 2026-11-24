"""Integration interfaces for SystemRuntime.

ILabwareStore: persistence for labware identity and relationships.
IAccessConfigStore: registry of named approach/retract patterns.
ITeachpointStore: registry of named transporter positions.
IDeckLayoutStore: registry of named PLR deck configurations per liquid handler.
IDeploymentProfileStore: registry of named variable-default bundles applied at submit.
"""

from typing import List, Protocol, Tuple

from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import AccessConfig, Teachpoint

from orca.resource_models.labware import LabwareInstance
from orca.variables.deployment_profile import DeploymentProfile


__all__ = [
    "ILabwareStore",
    "IAccessConfigStore",
    "IMoveDefaultsStore",
    "IGripProfileStore",
    "ITeachpointStore",
    "IDeckLayoutStore",
    "IDeploymentProfileStore",
]


class ILabwareStore(Protocol):
    """Persistence for labware identity and location. Only ``LabwareService`` holds one."""

    async def register(self, instance: LabwareInstance, execution_id: str | None = None) -> None:
        """Raises ``BarcodeInUseError`` when another labware already holds the barcode."""
        ...
    async def get_by_id(self, labware_id: str) -> LabwareInstance | None: ...
    async def get_by_barcode(self, barcode: str) -> LabwareInstance | None: ...
    async def update_location(self, labware_id: str, position_id: str) -> None: ...
    async def get_by_position(self, position_id: str) -> LabwareInstance | None:
        """The labware persisted at ``position_id``, or None.

        Reuse-bind consults this so a resident reagent rebinds to its existing
        stable identity (anchored to the physical container) instead of minting
        a fresh id when the in-memory slot is empty.

        Tie-break: if more than one labware is persisted at the position (moves
        do not clear a prior occupant's position), the **most recently
        registered** one wins. Every implementation must honor this so the
        in-memory and DB-backed stores rebind to the same identity."""
        ...
    async def list_active_locations(self) -> list[tuple[str, str]]: ...
    async def clear_location(self, labware_id: str) -> None:
        """Drop the labware's ACTIVE position, keeping its row and history.

        A lifecycle end (dispose at thread end, operator discharge) is not a
        retraction of the record: identity, location history and the ops trail
        stay queryable, but ``list_active_locations`` stops returning the
        labware so a reboot does not resurrect it at its end slot. Idempotent.
        """
        ...
    async def list_departed_locations(self) -> list[tuple[str, str]]:
        """(labware_id, last position_id) of every labware whose position was cleared."""
        ...
    async def get_location_history(
        self, labware_id: str,
    ) -> list[tuple[str, str | None, float]]: ...


class IMoveDefaultsStore(Protocol):
    """What a deployment has tuned about one transporter's moves.

    A layer of the move-parameter model, sitting between the built-in seed and
    the layers that narrow a single move (the site, the labware). It is stored
    rather than constant so an operator can change what "nothing specified"
    means for an arm without editing code.

    Sparse on purpose: the row holds only the fields somebody set, so a field
    nobody touched keeps following the seed and stays visibly untouched.

    `get` returns None for a transporter nobody has tuned. `set` is an upsert;
    `delete` returns True iff something was removed.

    `create_schema` / `aclose` are the persistence lifecycle hooks the Service
    calls for the store it owns; they are no-ops for a hosted deployment's
    store, where migrations own the schema and the app owns the engine.
    """

    async def get(self, transporter_name: str) -> MoveParameterPatch | None: ...
    async def list(self) -> dict[str, MoveParameterPatch]: ...
    async def set(
        self, transporter_name: str, patch: MoveParameterPatch,
    ) -> None: ...
    async def delete(self, transporter_name: str) -> bool: ...
    async def create_schema(self) -> None: ...
    async def aclose(self) -> None: ...


class IGripProfileStore(Protocol):
    """How each labware type is held, as a sparse patch over the arm's defaults.

    Layer two of the move-parameter model: what is being moved changes the move.
    A tip box and a deep well plate grip at different heights and want different
    jaw widths at the same taught position, and that follows the type wherever it
    goes rather than belonging to any one site.

    Sparse on purpose. A type that corrects its grip width names that field and
    nothing else, so a later edit to the arm's defaults still reaches everything
    this type did not have an opinion about.

    `get` returns None for a type with no profile, which means the layer
    contributes nothing and the move resolves exactly as it did before. `set` is
    an upsert; `delete` returns True iff something was removed.

    `create_schema` / `aclose` are the persistence lifecycle hooks the Service
    calls for the store it owns; they are no-ops for a hosted deployment's
    store, where migrations own the schema and the app owns the engine.
    """

    async def get(self, labware_type: str) -> MoveParameterPatch | None: ...
    async def list(self) -> dict[str, MoveParameterPatch]: ...
    async def set(self, labware_type: str, patch: MoveParameterPatch) -> None: ...
    async def delete(self, labware_type: str) -> bool: ...
    async def create_schema(self) -> None: ...
    async def aclose(self) -> None: ...


class IAccessConfigStore(Protocol):
    """Registry of named approach/retract patterns referenced by teachpoints.

    Operator CRUD and runtime lookups share a single store instance; the
    default is the ``access_configs.json`` file in the package, behind ``AccessConfigService``.

    `get` returns None for unknown names (the caller decides whether to
    fail or fall back). `add` raises on duplicate name; `update` raises on
    unknown name; `delete` returns True iff something was removed.

    `create_schema` / `aclose` are the persistence lifecycle hooks the
    Service calls for the in-memory default it owns; they are no-ops for a
    hosted store (Alembic owns the schema; the app owns the engine).
    """

    async def get(self, name: str) -> AccessConfig | None: ...
    async def list(self) -> List[AccessConfig]: ...
    async def add(self, config: AccessConfig) -> None: ...
    async def update(self, config: AccessConfig) -> None: ...
    async def delete(self, name: str) -> bool: ...
    async def create_schema(self) -> None: ...
    async def aclose(self) -> None: ...


class ITeachpointStore(Protocol):
    """Registry of named transporter positions. Pure CRUD.

    The store is the authoritative source of teachpoints. Path A: drivers
    hold a reference to the bound store and do `await self._store.get(name)`
    at every dispatch, so mid-run mutations are visible to the next move
    without an explicit refresh. Construction does not pre-load any
    driver-side cache.

    `resolve(name)` is the wire-source-of-truth lookup that returns a
    fully-flattened Teachpoint with access fields inlined. A hosted deployment
    uses this on the cloud side to materialize a wire-ready Teachpoint payload before
    dispatching `pick_at_coords` / `place_at_coords` / `move_to_coords` over
    the WebSocket so orca-client never resolves names locally. For
    in-memory and DB-backed stores the value is the same as `get` (the
    `Teachpoint` constructor inlines access fields), but the explicit name
    documents the wire contract.

    `get` returns None for unknown position_ids. `add` raises on duplicate
    position_id; `update` raises on unknown position_id; `delete` returns True
    iff something was removed.

    `create_schema` / `aclose` are the persistence lifecycle hooks the
    Service calls for the in-memory default it owns; they are no-ops for a
    hosted store (Alembic owns the schema; the app owns the engine).
    """

    async def get(self, position_id: str) -> Teachpoint | None: ...
    async def resolve(self, position_id: str) -> Teachpoint | None: ...
    async def list(self) -> List[Teachpoint]: ...
    async def add(self, teachpoint: Teachpoint) -> None: ...
    async def update(self, teachpoint: Teachpoint) -> None: ...
    async def delete(self, position_id: str) -> bool: ...
    async def create_schema(self) -> None: ...
    async def aclose(self) -> None: ...


class IDeckLayoutStore(Protocol):
    """Registry of named PLR deck configurations for a single liquid handler.

    Unlike teachpoints, deck layouts are rebuild-required: a running
    LiquidHandler has its deck configured once at SystemRuntime.start and
    cannot be reconfigured mid-run because physical labware can't safely
    reposition while motion is in flight. Edits to the registry take
    effect on the next system rebuild.

    `get` returns None for unknown names. `add` raises on duplicate name;
    `update` raises on unknown name; `delete` returns True iff something
    was removed. `build_system` awaits async `get` to derive child Locations
    for `LiquidHandler`, reading the live store with no snapshot
    in front of it.

    `create_schema` / `aclose` are the persistence lifecycle hooks the
    Service calls for the in-memory default it owns; they are no-ops for a
    hosted store (Alembic owns the schema; the app owns the engine).
    """

    async def get(self, name: str) -> DeckLayoutConfig | None: ...
    async def list(self) -> List[Tuple[str, DeckLayoutConfig]]: ...
    async def add(self, name: str, config: DeckLayoutConfig) -> None: ...
    async def update(self, name: str, config: DeckLayoutConfig) -> None: ...
    async def delete(self, name: str) -> bool: ...
    async def create_schema(self) -> None: ...
    async def aclose(self) -> None: ...


class IDeviceSetupStore(Protocol):
    """Each device's setup, read fresh on every call. A device with no setup gets the default one."""

    @property
    def default_client(self) -> str:
        """The orca-client that runs a device whose setup names none."""
        ...

    def names(self) -> list[str]: ...

    def read(self, name: str) -> DeviceSetup: ...


class IDeploymentProfileStore(Protocol):
    """Registry of named DeploymentProfile bundles.

    Profiles and the variable store stay categorically separate at runtime:
    ``runtime.profile_store.get(name)`` produces a profile, which the submission
    pipeline hands to ``variable_store.load_profile(execution_id, profile)``
    once at submit time. The variable store is the only resolver after that.

    Editing a profile via the registry does NOT affect a running execution.
    The new body is picked up at the NEXT submission that names this
    profile.

    Operator CRUD and runtime resolution share a single store instance.
    The source-available impl is file-backed (directory-of-JSONs); a hosted deployment
    ships a DB-backed impl.

    `get` returns None for unknown names. `add` raises on duplicate name;
    `update` raises on unknown name; `delete` returns True iff something
    was removed.
    """

    async def get(self, name: str) -> DeploymentProfile | None: ...
    async def list(self) -> List[DeploymentProfile]: ...
    async def add(self, profile: DeploymentProfile) -> None: ...
    async def update(self, profile: DeploymentProfile) -> None: ...
    async def delete(self, name: str) -> bool: ...
