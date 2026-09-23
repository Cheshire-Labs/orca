"""DeckLayoutFacade: external CRUD over per-liquid-handler
`IDeckLayoutStore` instances.

REST/MCP routes through `runtime.deck_layouts.<method>(device_id, ...)`.
The facade looks up the liquid handler on the System graph and
delegates to its bound `deck_layout_store`. There is exactly one
deck-layout registry per (device, deployment) at any moment.

Deck-layout edits are **rebuild-required**: a running LiquidHandler
has its deck configured once at SystemRuntime.start and cannot safely
reconfigure mid-run because physical labware can't reposition while
motion is in flight. The facade writes the registry; the next
`RuntimeLifecycle.rebuild()` picks up the new layout. REST/MCP
response bodies should communicate this contract -- the facade itself
does not raise on rebuild-required edits since the store is writable.

`KeyError` from `get_resource` (unknown device_id) and
`KeyError` from "resource exists but is not a LiquidHandler" both
propagate so REST/MCP can translate to 404.
"""

from typing import List, Protocol, Sequence, Tuple, runtime_checkable

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig

from orca.devices.devices import LiquidHandler
from orca.resource_models.resources import IResource
from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.interfaces import IDeckLayoutStore
from orca.runtime.runtime_interface import IDeckLayoutFacade


@runtime_checkable
class _ResourceHost(Protocol):
    """Narrow ISystem subset the facade actually uses.

    The full ISystem ABC carries 60+ abstract members; the facade only
    needs `get_resource` plus the ``devices`` iterable for the list-all
    walk. Depending on this narrow Protocol keeps test fakes lightweight
    and avoids `# type: ignore` on the call site.
    """
    def get_resource(self, name: str) -> IResource: ...

    @property
    def devices(self) -> Sequence[IResource]: ...


class DeckLayoutFacade(IDeckLayoutFacade):
    """Concrete IDeckLayoutFacade implementation."""

    def __init__(self, system: _ResourceHost) -> None:
        self._system = system

    async def get(
        self, device_id: str, name: str,
    ) -> DeckLayoutConfig | None:
        return await self._store_for(device_id).get(name)

    async def list(
        self, device_id: str,
    ) -> List[Tuple[str, DeckLayoutConfig]]:
        return await self._store_for(device_id).list()

    async def list_all(
        self,
    ) -> List[Tuple[str, str, DeckLayoutConfig]]:
        """Cross-liquid-handler walk -- yields ``(device_id, layout_name,
        config)`` for every LiquidHandler on the system. Empty stores
        contribute zero rows. Stable ordering by device declaration.
        """
        results: List[Tuple[str, str, DeckLayoutConfig]] = []
        for device in self._system.devices:
            if not isinstance(device, LiquidHandler):
                continue
            for name, config in await device.deck_layout_store.list():
                results.append((device.name, name, config))
        return results

    @dangerous(
        name="deck_layouts.add",
        level=DangerLevel.OPERATOR,
        message="Register a new deck layout '{name}' for liquid handler "
                "'{device_id}'. Takes effect on the next runtime "
                "rebuild; the running deck is unaffected until then.",
    )
    async def add(
        self, device_id: str, name: str, config: DeckLayoutConfig,
    ) -> None:
        await self._store_for(device_id).add(name, config)

    @dangerous(
        name="deck_layouts.update",
        level=DangerLevel.CRITICAL,
        message="Update deck layout '{name}' for liquid handler "
                "'{device_id}'. Rebuild-required: a running LiquidHandler "
                "keeps the previous layout until the next "
                "RuntimeLifecycle.rebuild() picks up the change.",
    )
    async def update(
        self, device_id: str, name: str, config: DeckLayoutConfig,
    ) -> None:
        await self._store_for(device_id).update(name, config)

    @dangerous(
        name="deck_layouts.delete",
        level=DangerLevel.CRITICAL,
        message="Delete deck layout '{name}' for liquid handler "
                "'{device_id}'. Rebuild-required to take effect; if the "
                "running LiquidHandler was configured with this layout, "
                "the next rebuild will fail to resolve it.",
    )
    async def delete(self, device_id: str, name: str) -> bool:
        return await self._store_for(device_id).delete(name)

    def _store_for(self, device_id: str) -> IDeckLayoutStore:
        return self._get_liquid_handler(device_id).deck_layout_store

    def _get_liquid_handler(self, device_id: str) -> LiquidHandler:
        try:
            resource = self._system.get_resource(device_id)
        except KeyError as exc:
            raise KeyError(
                f"liquid handler {device_id!r} not found",
            ) from exc
        if not isinstance(resource, LiquidHandler):
            raise KeyError(
                f"resource {device_id!r} is not a liquid handler",
            )
        return resource
