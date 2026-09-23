"""TeachpointFacade: the external CRUD surface over per-transporter
`ITeachpointStore` instances.

REST/MCP routes through `runtime.teachpoints.<method>(device_id, ...)`
rather than against any standalone repository class. The facade looks
up the transporter on the System graph and delegates to its bound
`teachpoint_store` -- the same store the runtime resolves against at
dispatch time. There is exactly one teachpoint registry per
(device, deployment) at any moment.

Reads pass through. Writes inherit whatever safety contract the
underlying store implements: the source-available InMemory store is permissive
(deletes succeed when the name exists); a hosted deployment's DB-backed store enforces
uniqueness via `(device_id, name)` and may raise additional errors at
safety boundaries the facade does not know about. The facade does not
add a second layer of validation; the store is the source of truth.

`KeyError` from `get_transporter` (unknown device_id) propagates so
REST/MCP can translate to 404.
"""

from typing import List, Protocol, Sequence, Tuple, runtime_checkable

from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch

from orca.runtime.move_parameters import reject_contradiction
from cheshire_drivers.teachpoints import Teachpoint

from orca.resource_models.transporter import Transporter
from orca.system.system_map import SystemMap
from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.interfaces import ITeachpointStore
from orca.runtime.runtime_interface import ITeachpointFacade


@runtime_checkable
class _TransporterHost(Protocol):
    """Narrow ISystem subset the facade actually uses.

    The full ISystem ABC carries 60+ abstract members; the facade only
    needs `get_transporter` plus the ``transporters`` iterable for the
    list-all walk, and the map a new teachpoint has to reach. Depending on
    this narrow Protocol keeps test fakes lightweight and avoids
    `# type: ignore` on the call site.
    """
    def get_transporter(self, name: str) -> Transporter: ...

    @property
    def transporters(self) -> List[Transporter]: ...

    @property
    def system_map(self) -> SystemMap: ...


class TeachpointFacade(ITeachpointFacade):
    """Concrete ITeachpointFacade implementation."""

    def __init__(self, system: _TransporterHost) -> None:
        self._system = system

    async def get(
        self, device_id: str, position_id: str,
    ) -> Teachpoint | None:
        return await self._store_for(device_id).get(position_id)

    async def list(self, device_id: str) -> List[Teachpoint]:
        return await self._store_for(device_id).list()

    async def list_all(self) -> List[Tuple[str, Teachpoint]]:
        """Cross-transporter walk -- yields ``(device_id, teachpoint)``
        for every transporter on the system that has a teachpoint store.
        Empty teachpoint stores contribute zero rows. Stable ordering by
        transporter declaration order.
        """
        results: List[Tuple[str, Teachpoint]] = []
        for transporter in self._system.transporters:
            for tp in await transporter.teachpoint_store.list():
                results.append((transporter.name, tp))
        return results

    @dangerous(
        name="teachpoints.add",
        level=DangerLevel.OPERATOR,
        message="Register a new teachpoint '{teachpoint.position_id}' for "
                "device '{device_id}'. The driver picks up the new "
                "coordinates on the next dispatch lookup.",
    )
    async def add(
        self, device_id: str, teachpoint: Teachpoint,
    ) -> None:
        transporter = self._get_transporter(device_id)
        await transporter.teachpoint_store.add(teachpoint)
        # The route graph is built from the teachpoints that existed at
        # startup. Wire the new one in now, or the arm that was just taught
        # this position cannot plan a move to it.
        await self._system.system_map.add_taught_position(
            transporter, teachpoint.position_id,
        )

    @dangerous(
        name="teachpoints.update",
        level=DangerLevel.CRITICAL,
        message="Update teachpoint '{teachpoint.position_id}' for device "
                "'{device_id}'. The driver picks up the new coordinates "
                "on the next dispatch lookup; in-flight motions to the "
                "old coordinates are NOT auto-cancelled.",
    )
    async def update(
        self, device_id: str, teachpoint: Teachpoint,
    ) -> None:
        transporter = self._get_transporter(device_id)
        await transporter.teachpoint_store.update(teachpoint)
        # Coordinates live in the store, not in the edges, so a re-teach leaves
        # the graph alone. This covers an update that names a position the
        # graph never had.
        await self._system.system_map.add_taught_position(
            transporter, teachpoint.position_id,
        )

    @dangerous(
        name="teachpoints.delete",
        level=DangerLevel.CRITICAL,
        message="Delete teachpoint '{position_id}' for device '{device_id}'. "
                "Workflows that move to this position_id will fail at "
                "dispatch time after deletion.",
    )
    async def delete(self, device_id: str, position_id: str) -> bool:
        return await self._store_for(device_id).delete(position_id)

    @dangerous(
        name="teachpoints.set_taught_with",
        level=DangerLevel.PHYSICAL,
        message="Record that '{position_id}' on device '{device_id}' was taught "
                "with labware '{labware_type}'. Grip heights stated relative to "
                "what a position was taught with are read against this, so "
                "naming the wrong labware moves every grip at this position.",
    )
    async def set_taught_with(
        self, device_id: str, position_id: str, labware_type: str | None,
    ) -> Teachpoint:
        """Name the labware this position was jogged to, or `None` to unsay it."""
        store = self._store_for(device_id)
        existing = await self._require(store, device_id, position_id)
        existing.taught_with = labware_type
        await store.update(existing)
        return existing

    @dangerous(
        name="teachpoints.apply_labware_override",
        level=DangerLevel.PHYSICAL,
        message="Change how labware '{labware_type}' is handled at position "
                "'{position_id}' on device '{device_id}'. This is the narrowest "
                "layer there is: it wins over the labware's own profile and over "
                "the arm's defaults, and it takes effect on the next move.",
    )
    async def apply_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
        patch: MoveParameterPatch,
        clear: Sequence[MoveParameterField] = (),
    ) -> Teachpoint:
        """Merge an exception for one labware type at one position.

        Setting and clearing land in one write for the same reason the grip
        profiles do it: a move that happened between two writes would resolve
        against a mixture neither call intended. An override edited down to
        nothing is dropped, so "no exception here" has one representation.
        """
        reject_contradiction(patch, clear)
        store = self._store_for(device_id)
        existing = await self._require(store, device_id, position_id)
        merged = patch.over(
            existing.by_labware.get(labware_type, MoveParameterPatch()),
        ).without(clear)
        if merged.model_dump(exclude_none=True):
            existing.by_labware[labware_type] = merged
        else:
            existing.by_labware.pop(labware_type, None)
        await store.update(existing)
        return existing

    @dangerous(
        name="teachpoints.clear_labware_override",
        level=DangerLevel.PHYSICAL,
        message="Drop the exception for labware '{labware_type}' at position "
                "'{position_id}' on device '{device_id}'. That labware goes back "
                "to being handled the way every other labware is here.",
    )
    async def clear_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
    ) -> bool:
        """Remove one labware's exception. False when there was none."""
        store = self._store_for(device_id)
        existing = await self._require(store, device_id, position_id)
        if labware_type not in existing.by_labware:
            return False
        del existing.by_labware[labware_type]
        await store.update(existing)
        return True

    @staticmethod
    async def _require(
        store: ITeachpointStore, device_id: str, position_id: str,
    ) -> Teachpoint:
        teachpoint = await store.get(position_id)
        if teachpoint is None:
            raise KeyError(
                f"teachpoint {position_id!r} not found for device {device_id!r}",
            )
        return teachpoint

    def _store_for(self, device_id: str) -> ITeachpointStore:
        transporter = self._get_transporter(device_id)
        return transporter.teachpoint_store

    def _get_transporter(self, device_id: str) -> Transporter:
        try:
            return self._system.get_transporter(device_id)
        except KeyError as exc:
            raise KeyError(
                f"transporter {device_id!r} not found",
            ) from exc
        except ValueError as exc:
            raise KeyError(
                f"resource {device_id!r} is not a transporter",
            ) from exc
