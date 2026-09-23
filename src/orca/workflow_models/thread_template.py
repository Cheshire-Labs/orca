from dataclasses import dataclass

from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.spawn import (
    DISPENSE,
    LEAVE_IN_PLACE,
    MANUAL_PLACE,
    MANUAL_REMOVE,
    REUSE_EXISTING,
    _VALID_END_SENTINELS,
    _VALID_START_SENTINELS,
)
from orca.workflow_models.method_template import IMethodTemplate

from typing import AsyncGenerator, Callable, List, Literal

from orca.workflow_models.thread_context import ThreadContext

_ALL_SPAWN_SENTINELS = _VALID_START_SENTINELS | _VALID_END_SENTINELS

# Value -> the name an author imports. Spelled out rather than derived from
# the value, so a sentinel that is not just lower_snake of its own identifier
# cannot make an error message cite an import that does not exist.
_SENTINEL_NAMES = {
    REUSE_EXISTING: "REUSE_EXISTING",
    DISPENSE: "DISPENSE",
    MANUAL_PLACE: "MANUAL_PLACE",
    LEAVE_IN_PLACE: "LEAVE_IN_PLACE",
    MANUAL_REMOVE: "MANUAL_REMOVE",
}

assert set(_SENTINEL_NAMES) == _ALL_SPAWN_SENTINELS, (
    "every spawn sentinel needs an entry in _SENTINEL_NAMES; without one the "
    "validator raises KeyError instead of naming the fix"
)


def _reject_if_sentinel(value: Location | str, side: Literal["start", "end"]) -> None:
    """Refuse a sentinel written where a location belongs.

    Sentinels are plain strings, so one slipped in as a location would
    otherwise become a candidate named e.g. "dispense": the author's intent
    is silently replaced by the default and the run dies far away on an
    unknown location. The message names the form that actually works,
    including when the sentinel belongs to the OTHER side.
    """
    if not isinstance(value, str) or value not in _ALL_SPAWN_SENTINELS:
        return
    name = _SENTINEL_NAMES[value]
    valid_here = (
        _VALID_START_SENTINELS if side == "start" else _VALID_END_SENTINELS
    )
    if value in valid_here:
        form = (
            f"start=(location, {name})" if side == "start"
            else f"end=([...], {name})"
        )
        raise ValueError(
            f"{value!r} is a spawn sentinel, not a location; it goes on the "
            f"tuple: {form}"
        )
    other, other_named = (
        ("end", "an end-side") if side == "start" else ("start", "a start-side")
    )
    other_form = (
        f"end=([...], {name})" if other == "end"
        else f"start=(location, {name})"
    )
    raise ValueError(
        f"{value!r} is {other_named} sentinel and is not valid on {side}=; "
        f"it goes on the other side: {other_form}"
    )

ThreadFunc = Callable[[ThreadContext], AsyncGenerator[IMethodTemplate, None]]

StartArg = Location | str | tuple[Location | str, str]
EndCandidates = Location | str | list[Location | str]
EndArg = EndCandidates | tuple[EndCandidates, str]


@dataclass(frozen=True)
class StartSpawnFlags:
    """Author-declared start-side spawn intent for a ThreadTemplate.

    Exactly one of the three flags (reuse_existing / dispense /
    manual_place) is True after `_normalize_start` runs;
    `_VALID_START_SENTINELS` policing makes any other tuple-form
    sentinel raise. Bare-string / Location default sets
    `manual_place=True`, so `start="loc"` and `start=("loc",
    MANUAL_PLACE)` produce the same flags and the same spawn dispatch.
    """
    reuse_existing: bool
    dispense: bool
    manual_place: bool


@dataclass(frozen=True)
class EndSpawnFlags:
    """Author-declared end-side spawn intent for a ThreadTemplate.

    Exactly one of the two flags is True after `_normalize_end` runs.
    Bare-string / Location default sets `manual_remove=True`.
    """
    leave_in_place: bool
    manual_remove: bool


def _normalize_start(start: StartArg) -> tuple[Location | str, StartSpawnFlags]:
    """Unpack a `@orca.thread(start=...)` value into (location, flags).

    Accepts three forms:
      - bare string ("pad_1") -- defaults to ManualPlace (operator places
        labware at the slot; sim modes auto-fulfill, LIVE parks the thread
        at AWAITING_MANUAL_PLACE).
      - Location object -- same default as bare string.
      - tuple (loc, REUSE_EXISTING) -- bind to existing labware at the slot
        (deck-resident labware shared across executions).
      - tuple (loc, DISPENSE) -- IPlateSource-backed source dispenses the
        next plate physically; engine writes the slot after the device call.
      - tuple (loc, MANUAL_PLACE) -- explicit form of the bare-string
        default.
    """
    if isinstance(start, tuple):
        if len(start) != 2:
            raise ValueError(
                f"start tuple must be (location, sentinel); got {start!r}"
            )
        loc, sentinel = start
        if sentinel not in _VALID_START_SENTINELS:
            raise ValueError(
                f"start tuple sentinel must be one of "
                f"{sorted(_VALID_START_SENTINELS)}; got {sentinel!r}"
            )
        _reject_if_sentinel(loc, "start")
        return loc, StartSpawnFlags(
            reuse_existing=sentinel == REUSE_EXISTING,
            dispense=sentinel == DISPENSE,
            manual_place=sentinel == MANUAL_PLACE,
        )
    _reject_if_sentinel(start, "start")
    return start, StartSpawnFlags(
        reuse_existing=False, dispense=False, manual_place=True,
    )


def _as_candidate_list(value: EndCandidates) -> list[Location | str]:
    candidates = list(value) if isinstance(value, list) else [value]
    if not candidates:
        raise ValueError("end needs at least one location")
    for candidate in candidates:
        if not isinstance(candidate, (Location, str)):
            raise ValueError(
                f"end candidates must be locations or names; got {candidate!r} "
                f"(sentinels go on the tuple: end=([...], SENTINEL))"
            )
        _reject_if_sentinel(candidate, "end")
    return candidates


def _normalize_end(end: EndArg) -> tuple[list[Location | str], EndSpawnFlags]:
    """Unpack a `@orca.thread(end=...)` value into (candidate locations, flags).

    Accepts these forms:
      - bare string ("pad_1") or Location -- defaults to ManualRemove
        (operator removes labware from the slot at thread end; sim modes
        auto-dispose, LIVE parks the thread at AWAITING_MANUAL_REMOVE).
      - list of the above -- interchangeable candidate spots (a hotel's
        shelves); the thread ends at whichever is granted. Route score
        picks the winner, not the order the candidates are written in.
      - tuple (loc-or-list, LEAVE_IN_PLACE) -- engine does NOT dispose at
        thread end; paired with REUSE_EXISTING for deck-resident labware
        that outlives any single execution.
      - tuple (loc-or-list, MANUAL_REMOVE) -- explicit form of the default.
    """
    if isinstance(end, tuple):
        if len(end) != 2:
            raise ValueError(
                f"end tuple must be (location, sentinel); got {end!r}"
            )
        loc, sentinel = end
        if sentinel not in _VALID_END_SENTINELS:
            raise ValueError(
                f"end tuple sentinel must be one of "
                f"{sorted(_VALID_END_SENTINELS)}; got {sentinel!r}"
            )
        return _as_candidate_list(loc), EndSpawnFlags(
            leave_in_place=sentinel == LEAVE_IN_PLACE,
            manual_remove=sentinel == MANUAL_REMOVE,
        )
    return _as_candidate_list(end), EndSpawnFlags(
        leave_in_place=False, manual_remove=True,
    )


class ThreadTemplateNameCollisionError(KeyError):
    """Two distinct ``ThreadTemplate`` objects share the same name within one workflow.

    Thread names are unique per workflow, not deployment-wide: two
    workflows may each declare a ``sample_plate`` thread. The collision
    fires only when two distinct thread objects claim the same name inside
    the same workflow's bundle. Inherits ``KeyError`` for backward compat:
    every pre-existing catcher of ``KeyError`` from the registry-add path
    continues to handle the case. The typed fields (``template_name`` plus
    optional workflow attributions) let API surfaces produce a typed
    ``thread_template_name_collision`` envelope so AI agents and operator
    portals can branch on the failure category instead of regex-matching
    the human message.
    """

    def __init__(
        self,
        template_name: str,
        *,
        conflicting_workflow: str | None = None,
        existing_workflow: str | None = None,
        message: str | None = None,
    ) -> None:
        self.template_name = template_name
        self.conflicting_workflow = conflicting_workflow
        self.existing_workflow = existing_workflow
        if message is None:
            message = (
                f"Thread template name collision: '{template_name}' "
                "already registered. Each thread name must be unique "
                "within the workflow."
            )
        super().__init__(message)


class DeckSiteEndRequiresLeaveInPlaceError(RuntimeError):
    """A thread ends at a device-internal site (e.g. a deck site
    "lh/carrier-25-0") with an end intent other than LEAVE_IN_PLACE.

    Thread completion routes labware to the device handoff, never the deck
    site itself. There is no intra-device move that places the labware on the
    specific site yet, so a non-LEAVE_IN_PLACE end would dispose against the
    empty site while the labware sits at the handoff: the dispose no-ops and
    the labware never clears. Only LEAVE_IN_PLACE is
    coherent for such an end (a deck-resident reagent already on its site stays
    there, no move, no dispose). The moved-in case is refused until
    intra-device placement lands.
    """

    def __init__(self, thread_name: str, position_id: str) -> None:
        self.thread_name = thread_name
        self.position_id = position_id
        super().__init__(
            f"Thread '{thread_name}' ends at device-internal location "
            f"'{position_id}' without LEAVE_IN_PLACE. Thread completion routes "
            f"labware to the device handoff, not the specific site, so a "
            f"dispose against the site would no-op and never clear the labware. "
            f"Use end=('{position_id}', LEAVE_IN_PLACE) for a deck-resident "
            f"reagent, or end the thread at a top-level location."
        )


class ThreadTemplate:
    """Template for a thread: an async generator yielding methods for a labware.

    Created via @orca.thread decorator or ThreadTemplate(labware, start, end, func=...).
    start/end can be Location objects or string names (resolved by build_system).
    Only the decorator path registers the template for catalog discovery; direct
    construction produces a template object without side effects.
    """

    def __init__(self,
                 labware_template: LabwareTemplate,
                 start: StartArg,
                 end: EndArg,
                 func: ThreadFunc,
                 contributes_to: list[str] | None = None,
                 required: bool = True,
                 immovable: bool = False,
                 ) -> None:
        start_loc, start_flags = _normalize_start(start)
        end_loc, end_flags = _normalize_end(end)
        self._start_reuse_existing: bool = start_flags.reuse_existing
        self._start_dispense: bool = start_flags.dispense
        self._end_leave_in_place: bool = end_flags.leave_in_place
        self._end_manual_remove: bool = end_flags.manual_remove
        self._labware_template: LabwareTemplate = labware_template
        self._start: Location | str = start_loc
        self._ends: list[Location | str] = end_loc
        self._func: ThreadFunc = func
        self._contributes_to: list[str] = list(contributes_to) if contributes_to else []
        self._required: bool = required
        self._immovable: bool = immovable
        # Function's __name__ -- the user-facing identifier operators tend to
        # write in LabwareGroupMember.thread_template_name. Falls back to the
        # labware name when the function has no __name__ attribute (e.g.,
        # lambdas).
        fn_name = getattr(func, "__name__", None)
        self._func_name: str = (
            fn_name if fn_name and fn_name != "<lambda>" else labware_template.name
        )

    @property
    def name(self) -> str:
        return self._labware_template.name

    @property
    def labware_template(self) -> LabwareTemplate:
        return self._labware_template

    @property
    def start_location(self) -> Location:
        if isinstance(self._start, str):
            raise ValueError(f"Start location '{self._start}' has not been resolved. Call build_system() first.")
        return self._start

    @property
    def end_locations(self) -> list[Location]:
        resolved: list[Location] = []
        for end in self._ends:
            if isinstance(end, str):
                raise ValueError(f"End location '{end}' has not been resolved. Call build_system() first.")
            resolved.append(end)
        return resolved

    @property
    def start_position_id(self) -> str:
        """position_id of the start location, whether resolved or still a string reference."""
        return self._start if isinstance(self._start, str) else self._start.position_id

    @property
    def end_position_ids(self) -> list[str]:
        """position_id of each end candidate, whether resolved or still a string reference."""
        return [
            end if isinstance(end, str) else end.position_id
            for end in self._ends
        ]

    @property
    def func(self) -> ThreadFunc:
        return self._func

    @property
    def contributes_to(self) -> list[str]:
        """The labware template name of each receiver this thread feeds.

        Declared on @orca.thread(contributes_to=[...]) using the receiver's
        ``labware=`` template name (not its thread-function name). When every
        live thread declaring a given receiver has terminated, that receiver's
        slot closes (see ExecutingWorkflow._on_thread_work_finished).
        """
        return list(self._contributes_to)

    @property
    def func_name(self) -> str:
        """Name of the decorated function (``@orca.thread`` target).

        Operators typically write this string in
        ``LabwareGroupMember.thread_template_name`` because it matches how
        they reference the thread in code. Submit-time validation accepts
        either ``func_name`` or the labware template's ``name``.
        """
        return self._func_name

    @property
    def end_leave_in_place(self) -> bool:
        """True when the thread skips auto-dispose at its end_location.

        Set via tuple form on `@orca.thread(end=("loc", LEAVE_IN_PLACE))`.
        `_handle_thread_completion` consults this flag to decide whether to
        dispose the labware. Deck-resident reagent labware sets this so the
        instance survives past its first thread's end and the next
        submission's reuse-bind path can re-attach.
        """
        return self._end_leave_in_place

    @property
    def start_reuse_existing(self) -> bool:
        """True when the thread binds to existing labware at start_location.

        Set via tuple form on `@orca.thread(start=("loc", REUSE_EXISTING))`.
        The auto-spawn callback checks this flag; the pre-submission
        start_location check skips threads that set it (the binding logic
        handles occupancy). Threads with this flag set CANNOT be registered
        as `wf.start()` entries -- enforced at build time in
        WorkflowTemplate.add_thread.
        """
        return self._start_reuse_existing

    @property
    def start_dispense(self) -> bool:
        """True when start_location is dispensed from by an IPlateSource.

        Set via tuple form on `@orca.thread(start=("loc", DISPENSE))`.
        The engine routes this thread to `DispenseSpawn`, which calls
        `device.dispense()` on the source backing the location before the
        slot is written.
        """
        return self._start_dispense

    @property
    def end_manual_remove(self) -> bool:
        """True when an operator removes labware from end_location at thread end.

        Bare-string / Location form defaults to this. Explicit form is
        `@orca.thread(end=("loc", MANUAL_REMOVE))`. The engine routes
        this thread's completion to `ManualRemoveSpawn`: sim modes
        auto-dispose; LIVE mode parks the thread at
        `AWAITING_MANUAL_REMOVE` until `labware_discharge` fires.

        Mutually exclusive with `end_leave_in_place` -- exactly one is
        True for every thread template.
        """
        return self._end_manual_remove

    @property
    def required(self) -> bool:
        """Whether a submission must provide a member for this thread.

        Default True: every group (for PER_GROUP labware) must include a
        member for this thread_template_name, else submit() rejects. When
        False, the member may be omitted and the thread simply does not
        spawn for groups that don't name it.
        """
        return self._required

    @property
    def immovable(self) -> bool:
        """Author assertion that this thread will not move its labware off start_location.

        Set via `@orca.thread(immovable=True)`. The reservation system's
        deadlock detector (`ThreadDeadlockDetector.find_unresolvable_blocker`)
        treats this as a terminal signal: when another thread requests a
        reservation blocked by this thread's labware, the detector attaches
        an `UnresolvableDeadlockContext` to the requester's collection and
        signals the `unresolvable_deadlock` event. The resolver
        (`ResourcePoolResolver.resolve_action_location` for actions,
        `MoveHandler._resolve_reservation_from_move_action_collection`
        for moves) then raises `UnresolvableDeadlockError` instead of
        looping in the rejection-retry path.

        Distinct from `start == end` (a "stationary" thread that may still
        move labware around internally). Immovability is a system-level
        assertion that no thread will move the labware.

        Cannot be combined with `wf.start()` -- enforced at build time by
        `WorkflowTemplate.add_thread`. An immovable entry thread would
        deadlock against itself on its own first action.
        """
        return self._immovable

    def resolve_locations(self, get_location: Callable[[str], Location]) -> None:
        """Resolve string location names to Location objects, then reject an
        end at a device-internal site that is not LEAVE_IN_PLACE."""
        start = self._start
        self._start = get_location(start) if isinstance(start, str) else start
        # Typed local is load-bearing: iterating self._ends does not narrow,
        # so the guard below would need an isinstance that skips silently.
        ends: list[Location] = [
            get_location(end) if isinstance(end, str) else end
            for end in self._ends
        ]
        self._ends = list(ends)
        # A deck slot is an operator-managed residency, not a disposal point:
        # ending there is only coherent as LEAVE_IN_PLACE (deck resident).
        from orca.resource_models.device_deck_site import DeviceDeckSite
        for end in ends:
            if isinstance(end.resource, DeviceDeckSite) and not self._end_leave_in_place:
                raise DeckSiteEndRequiresLeaveInPlaceError(self.name, end.position_id)


_PENDING_THREAD_TEMPLATES: List[ThreadTemplate] = []


def drain_pending_thread_templates() -> List[ThreadTemplate]:
    """Return and clear the list of ThreadTemplates awaiting catalog registration.

    Populated exclusively by `@orca.thread`, which calls `register_pending_thread`
    after constructing the template. Direct construction does NOT append --
    those templates participate via workflow traversal (they are added to a
    workflow and the builder walks the workflow) or are handed to the builder
    explicitly.
    """
    out = list(_PENDING_THREAD_TEMPLATES)
    _PENDING_THREAD_TEMPLATES.clear()
    return out


def register_pending_thread(template: ThreadTemplate) -> None:
    """Queue a thread template for catalog registration. Called by `@orca.thread`."""
    _PENDING_THREAD_TEMPLATES.append(template)
