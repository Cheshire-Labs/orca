"""Which driver worlds may disagree with Orca's record, and the one step that rebuilds them before a command."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from enum import Enum

from orca.devices.deck_gripper_transporter import DeckGripperTransporter
from orca.devices.devices import LiquidHandler
from orca.gateway.controller.exceptions import CommandNotSavedError
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_registry import IResourceRegistry
from orca.resource_models.resources import IInitializable
from orca.resource_models.transporter import Transporter

WorldDevice = LiquidHandler | Transporter
RebuildWorld = Callable[[WorldDevice], Awaitable[bool]]
"""Rebuild a device's world, in the mode in force, from the record. False when it could not rebuild yet."""

_EVERY_MODE = tuple(WorkflowRunMode)

World = tuple[str, WorkflowRunMode]

logger = logging.getLogger(__name__)


class _Mark(Enum):
    BEHIND_THE_RECORD = "behind_the_record"
    """The record changed and this world was not told. Nothing else about it is in doubt."""
    IN_DOUBT = "in_doubt"
    """The driver may hold anything: a command got no answer, or its session may have been rebuilt."""


class DeviceWorlds:
    """Each liquid handler and arm keeps one driver world per run mode, and a write lands on one of them.

    A world that may no longer match the record is marked. The next command to that device rebuilds it first,
    whoever sends the command, so a run, an operator and the engine all start on a world that matches the record.
    Every world starts marked: a new system has told no driver anything yet.

    A mark says whether the world is only behind the record or in doubt. A world in doubt stays in doubt until it is
    rebuilt, whatever is marked on it after.
    """

    def __init__(self, resources: IResourceRegistry, rebuild: RebuildWorld) -> None:
        self._resources = resources
        self._rebuild = rebuild
        self._owing: dict[World, _Mark] = {}
        self.mark_everything()

    def mark(self, device_name: str, mode: WorkflowRunMode) -> None:
        self._mark([(device_name, mode)], _Mark.IN_DOUBT)

    def mark_every_world(self, device_name: str) -> None:
        """The device's client rebuilt its drivers, or the device came back: no world it holds can be trusted."""
        self._mark(((device_name, mode) for mode in _EVERY_MODE), _Mark.IN_DOUBT)

    def _mark(self, worlds: Iterable[World], mark: _Mark) -> None:
        for world in worlds:
            if self._owing.get(world) is not _Mark.IN_DOUBT:
                self._owing[world] = mark

    def mark_everything(self) -> None:
        for device in self.devices():
            self.mark_every_world(device.name)

    async def mark_current_world(self, device_name: str) -> None:
        """A command sent to this handler failed, got no answer or was not saved: its deck may hold anything.

        An arm's world is left to the move's own recovery: releasing the jaws resets it, and an arm world holds one
        plate per position, so rebuilding it mid-run would drop a plate that shares a site.
        """
        device = self._world_device(device_name)
        if isinstance(device, LiquidHandler):
            self.mark(device.name, await device.effective_mode())

    async def mark_current_worlds(self) -> None:
        """A fresh run starts on worlds rebuilt from the record, as the mode in force resolves each device."""
        for device in self.devices():
            self.mark(device.name, await device.effective_mode())

    async def sent_to(self, device_name: str) -> None:
        device = self._world_device(device_name)
        if device is not None:
            await self._mark_the_others_of(device)

    async def mark_every_deck_and_other_world(self) -> None:
        """Labware left that no slot points at, or every labware did: every world but an arm's in force is marked.

        An arm's world in force is left to the next run's start: rebuilding it mid-run drops a plate that shares
        a site.
        """
        for device in self.devices():
            if isinstance(device, LiquidHandler):
                self._mark(((device.name, mode) for mode in _EVERY_MODE), _Mark.BEHIND_THE_RECORD)
            else:
                await self._mark_the_others_of(device)

    async def mark_other_worlds(self) -> None:
        """A write reached the world in force on each device; every other world still holds what it held."""
        for device in self.devices():
            await self._mark_the_others_of(device)

    async def _mark_the_others_of(self, device: WorldDevice) -> None:
        current = await device.effective_mode()
        self._mark(((device.name, mode) for mode in _EVERY_MODE if mode is not current), _Mark.BEHIND_THE_RECORD)

    async def owes_a_rebuild(self, device: WorldDevice) -> bool:
        return (device.name, await device.effective_mode()) in self._owing

    async def bring_up_to_date_unless_in_doubt(self, device_name: str) -> None:
        """Rebuild the world in force when it is only behind the record. A world in doubt is left as it stands."""
        device = self._world_device(device_name)
        if device is None:
            return
        if self._owing.get((device.name, await device.effective_mode())) is _Mark.BEHIND_THE_RECORD:
            await self.bring_up_to_date(device_name)

    async def bring_up_to_date(self, device_name: str) -> None:
        """Rebuild this device's world in the mode in force, if it is marked. A deck gripper's is its handler's.

        The mark is cleared before the rebuild sends anything, so the rebuild's own commands find nothing owed and a
        mark set while it runs is kept. A rebuild that fails, or could not run yet, leaves the world marked. A device
        another task holds is left marked too: the command meets that lock itself, and a rebuild must not queue
        behind it or act on a world mid-command.
        """
        if not self._owing:
            return
        device = self._world_device(device_name)
        if device is None or (device.lock.locked() and not device.lock.held_by_this_task):
            return
        world = (device.name, await device.effective_mode())
        mark = self._owing.get(world)
        if mark is None:
            return
        with self._mark_cleared_while_rebuilding(world):
            rebuilt = await self._rebuild(device)
        if not rebuilt:
            self._mark([world], mark)

    @asynccontextmanager
    async def rebuilding(self, device_name: str) -> AsyncIterator[None]:
        """The caller rebuilds the device's world in force inside: its mark is cleared first, and set again if the
        rebuild fails. A mark set while the rebuild runs is kept."""
        device = self._world_device(device_name)
        if device is None:
            yield
            return
        with self._mark_cleared_while_rebuilding((device.name, await device.effective_mode())):
            yield

    @contextmanager
    def _mark_cleared_while_rebuilding(self, world: World) -> Iterator[None]:
        mark = self._owing.pop(world, _Mark.IN_DOUBT)
        try:
            yield
        except BaseException:
            self._mark([world], mark)
            raise

    async def after_a_lifecycle_command(self, device_name: str) -> None:
        """A device the command brought up has its world rebuilt now if it is marked.

        The lifecycle command itself answered, so a rebuild that fails does not fail it: the world stays marked and
        the next command to the device rebuilds it or fails.
        """
        device = self._world_device(device_name)
        if device is None or not (isinstance(device, IInitializable) and device.is_initialized):
            return
        try:
            await self.bring_up_to_date(device_name)
        except CommandNotSavedError:
            raise
        except Exception as exc:
            logger.warning("%s: its world was not rebuilt after it was brought up (%s); the next command retries.",
                           device.name, exc)

    def _world_device(self, device_name: str) -> WorldDevice | None:
        if not self._resources.has_resource(device_name):
            return None
        resource = self._resources.get_resource(device_name)
        if isinstance(resource, DeckGripperTransporter):
            return resource.device
        return resource if isinstance(resource, LiquidHandler | Transporter) else None

    def devices(self) -> list[WorldDevice]:
        """The devices whose driver keeps its own picture of where labware is: liquid handlers and arms."""
        return [r for r in self._resources.resources if isinstance(r, LiquidHandler | Transporter)]
