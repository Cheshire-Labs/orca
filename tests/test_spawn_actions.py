"""SpawnAction dispatch unit tests.

`select_spawn_action(thread)` reads the thread's `thread_template`
flags to pick a strategy:

- `template.start_dispense` -> :class:`DispenseSpawn`
- anything else -> :class:`ManualPlaceSpawn`, which is the default a bare
  string, a Location and an explicit MANUAL_PLACE all fall through to

`template.start_reuse_existing` is handled upstream in
`ExecutingWorkflow._resolve_reuse_bind`; threads that take the
reuse-bind path never reach `select_spawn_action`. Dispatch is
flag-based, NOT resource-based -- the author always declares spawn
intent. The auto-detect-by-IPlateSource path is
explicitly dropped.

Each `SpawnAction.acquire` takes the `LabwareThreadInstance` so the
strategy can read `run_mode` and (for ManualPlaceSpawn LIVE) rebind
`thread._labware`. The `acquire(labware)` signature is gone.
"""

from collections.abc import AsyncGenerator

import pytest

from orca.devices.devices import Storage
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.spawn import DISPENSE, MANUAL_PLACE
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.resource_models.labware_location_service import (
    InMemoryLabwareLocationService,
)
from orca.workflow_models.spawn_actions import (
    DispenseSpawn,
    ManualPlaceSpawn,
    select_spawn_action,
)
from orca.workflow_models.thread_template import ThreadFunc, ThreadTemplate
from tests.test_helpers import create_test_plate_template


def _make_platepad_location(name: str = "pad1") -> Location:
    pad = PlatePad(name)
    return Location(name, resource=pad)


def _make_storage_location(name: str = "stacker_1") -> tuple[Location, Storage]:
    storage = Storage(name)
    bridge = LabwareStagingBridge(name, storage)
    location = Location(name)
    location._resource = bridge
    return location, storage


def _fresh_labware(template_name: str = "plate_96") -> LabwareInstance:
    return LabwareInstance(template_name, "96_well")


def _trivial_func() -> ThreadFunc:
    async def fn(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        if False:
            yield
    return fn


def _make_thread_with_template(
    template: ThreadTemplate,
    labware: LabwareInstance,
    start_location: Location,
    *,
    run_mode: WorkflowRunMode = WorkflowRunMode.PURE_SIM,
) -> LabwareThreadInstance:
    thread = LabwareThreadInstance(
        labware=labware,
        start_location=start_location,
        end_locations=[start_location],
        run_mode=run_mode,
    )
    thread.set_thread_template(template)
    thread.set_labware_template(template.labware_template)
    return thread


class TestSelectSpawnActionByFlag:
    """Dispatch reads the template's start_* flags, not the location's
    resource shape."""

    def test_bare_string_start_dispatches_to_manual_place(self) -> None:
        plate = create_test_plate_template("plate_x")
        location = _make_platepad_location()
        template = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=_trivial_func(),
        )
        thread = _make_thread_with_template(template, _fresh_labware(), location)

        spawn = select_spawn_action(thread, InMemoryLabwareLocationService())
        assert isinstance(spawn, ManualPlaceSpawn)

    def test_explicit_manual_place_dispatches_to_manual_place(self) -> None:
        plate = create_test_plate_template("plate_x")
        location = _make_platepad_location()
        template = ThreadTemplate(
            labware_template=plate,
            start=("pad1", MANUAL_PLACE),
            end="pad1",
            func=_trivial_func(),
        )
        thread = _make_thread_with_template(template, _fresh_labware(), location)

        spawn = select_spawn_action(thread, InMemoryLabwareLocationService())
        assert isinstance(spawn, ManualPlaceSpawn)

    def test_explicit_dispense_dispatches_to_dispense_spawn(self) -> None:
        plate = create_test_plate_template("plate_x")
        location, _ = _make_storage_location()
        template = ThreadTemplate(
            labware_template=plate,
            start=("stacker_1", DISPENSE),
            end="stacker_1",
            func=_trivial_func(),
        )
        thread = _make_thread_with_template(template, _fresh_labware(), location)

        spawn = select_spawn_action(thread, InMemoryLabwareLocationService())
        assert isinstance(spawn, DispenseSpawn)

    def test_iplate_source_location_without_dispense_flag_dispatches_to_manual_place(
        self,
    ) -> None:
        """Resource-based auto-detection is GONE. An IPlateSource-backed
        location with a bare-string start defaults to ManualPlaceSpawn
        (the author did not declare DISPENSE). The author who wants
        physical dispense MUST write the explicit DISPENSE sentinel."""
        plate = create_test_plate_template("plate_x")
        location, _ = _make_storage_location()
        template = ThreadTemplate(
            labware_template=plate, start="stacker_1", end="stacker_1",
            func=_trivial_func(),
        )
        thread = _make_thread_with_template(template, _fresh_labware(), location)

        spawn = select_spawn_action(thread, InMemoryLabwareLocationService())
        assert isinstance(spawn, ManualPlaceSpawn)


class TestSpawnActionsRemovedSymbols:
    """`DefaultSpawn` is deleted entirely; `FromSourceSpawn` is renamed to
    `DispenseSpawn`. Importers of the old symbols should fail loudly so
    the renames don't silently linger."""

    def test_default_spawn_class_is_gone(self) -> None:
        import orca.workflow_models.spawn_actions as spawn_actions
        assert not hasattr(spawn_actions, "DefaultSpawn")

    def test_from_source_spawn_class_is_gone(self) -> None:
        import orca.workflow_models.spawn_actions as spawn_actions
        assert not hasattr(spawn_actions, "FromSourceSpawn")


class TestSelectEndSpawnActionTemplateLess:
    """Review item L4: a thread with `thread_template is None` must NOT
    be routed to ManualRemoveSpawn under LIVE. There is no
    author-declared end-side intent, so parking the thread at
    `AWAITING_MANUAL_REMOVE` forever is wrong.

    `select_end_spawn_action(thread)` returns None for the template-
    less case so `_handle_thread_completion` skips dispose entirely.
    Synthetic test threads and pre-template construction paths land
    here. Everything now routes through the strategy, so returning None
    is the cleaner LIVE-safe choice.
    """

    def test_returns_none_when_template_is_none(self) -> None:
        from orca.workflow_models.spawn_actions import select_end_spawn_action

        location = _make_platepad_location()
        thread = LabwareThreadInstance(
            labware=_fresh_labware(),
            start_location=location,
            end_locations=[location],
            run_mode=WorkflowRunMode.LIVE,
        )
        assert thread.thread_template is None
        assert select_end_spawn_action(thread, location) is None

    def test_returns_none_when_end_leave_in_place(self) -> None:
        """Pre-existing contract preserved: leave-in-place still
        returns None so deck-resident reagents survive thread end."""
        from orca.spawn import LEAVE_IN_PLACE, REUSE_EXISTING
        from orca.workflow_models.spawn_actions import select_end_spawn_action

        plate = create_test_plate_template("plate_leave")
        location = _make_platepad_location()
        template = ThreadTemplate(
            labware_template=plate,
            start=("pad1", REUSE_EXISTING),
            end=("pad1", LEAVE_IN_PLACE),
            func=_trivial_func(),
        )
        thread = _make_thread_with_template(template, _fresh_labware(), location)
        assert select_end_spawn_action(thread, location) is None

    def test_returns_manual_remove_spawn_for_bare_string_end(self) -> None:
        """Bare-string default -> ManualRemoveSpawn. The L4 fix does
        NOT regress the normal strategy dispatch."""
        from orca.workflow_models.spawn_actions import (
            ManualRemoveSpawn, select_end_spawn_action,
        )

        plate = create_test_plate_template("plate_dispose")
        location = _make_platepad_location()
        template = ThreadTemplate(
            labware_template=plate, start="pad1", end="pad1",
            func=_trivial_func(),
        )
        thread = _make_thread_with_template(template, _fresh_labware(), location)
        assert isinstance(select_end_spawn_action(thread, location), ManualRemoveSpawn)
