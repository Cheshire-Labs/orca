from typing import List, Optional


class ReuseThreadCannotBeEntryError(RuntimeError):
    """Raised at build time when a `start_reuse_existing` thread is registered
    via `wf.start()` instead of `wf.thread()`.

    `wf.start()` constructs the thread eagerly per submission via
    `build_entry_threads_for`. If a reuse-binding thread were registered
    that way, every submission would mint a fresh `LabwareThreadInstance`
    against the same bound labware -- multi-receiver, one-labware. Reuse
    threads must go through the auto-spawn slot machinery (`wf.thread()`)
    which guarantees exactly one receiver per slot.
    """

    def __init__(self, thread_name: str) -> None:
        self.thread_name = thread_name
        super().__init__(
            f"Thread '{thread_name}' has start_reuse_existing=True and cannot "
            f"be registered as a workflow entry via wf.start(). Use wf.thread() "
            f"so the auto-spawn slot machinery guarantees a single receiver "
            f"binds the persistent labware."
        )


class ImmovableThreadCannotBeEntryError(RuntimeError):
    """Raised at build time when a thread with `immovable=True` is registered
    via `wf.start()` instead of `wf.thread()`.

    An immovable thread asserts the engine will never move its labware
    off `start_location`. As a workflow entry, its first action against
    its own `start_location` would immediately fall into the
    unresolvable-deadlock declaration path the immovable flag is
    designed to fire -- the thread's own labware would be flagged as
    the blocker for the thread itself. Refused at build time so the
    operator catches the contradiction before submission.

    Symmetric with `ReuseThreadCannotBeEntryError` for the other
    thread-start intent that doesn't make sense as a workflow entry.
    """

    def __init__(self, thread_name: str) -> None:
        self.thread_name = thread_name
        super().__init__(
            f"Thread '{thread_name}' was registered as a workflow entry via "
            f"wf.start() with immovable=True. An immovable thread cannot "
            f"initiate a workflow because its first action against its own "
            f"start_location would deadlock against the immovable flag's own "
            f"contract. Wrap the thread in wf.thread() so an entry thread "
            f"spawns it, or drop immovable=True if the thread genuinely "
            f"needs to be an entry."
        )


class SpawnDidNotPlaceError(RuntimeError):
    """A spawn action returned without putting the thread's labware anywhere.

    An engine bug, not an operator one. Every spawn either places the labware
    (sim writes the slot, dispense advances the source, a LIVE manual place
    waits for the operator to register) or the thread was cooperatively
    stopped. Anything else means the ledger never saw the arrival, and letting
    the thread continue would run it against a driver deck that was never told
    about the plate -- surfacing much later as an unexplained pick.
    """

    def __init__(
        self,
        thread_name: str,
        labware_name: str,
        location: str,
        spawn: str,
        placement: str,
    ) -> None:
        self.thread_name = thread_name
        self.labware_name = labware_name
        self.location = location
        self.spawn = spawn
        self.placement = placement
        super().__init__(
            f"{spawn} returned for thread {thread_name!r} but {labware_name!r} "
            f"is still {placement} at {location!r}: the spawn never recorded an "
            f"arrival, so nothing placed the labware"
        )


class SpawnIncompatibleError(RuntimeError):
    """Raised when labware of the wrong template is offered at a location a
    thread is depending on.

    Two producers:

    - `ExecutingWorkflow._resolve_reuse_bind` raises when reuse-bind
      enters the location's `spawn_lock` and finds existing labware
      whose template name does not match `template.labware_template.name`.
      Operator pre-loaded the wrong trough; the binding refuses rather
      than shadowing.
    - `LabwareFacade.register` raises when a thread is waiting for a manual
      place at that slot and the operator offers a different template. The
      refusal lands on the operator's own call, before anything is written, so
      they can correct it -- rather than the thread failing behind them.

    A typed envelope is friendlier than the alternative -- falling back to
    create-fresh would silently shadow the operator's labware, and stalling
    on `DeviceBusyError` would hide the cause.
    """

    def __init__(
        self,
        location: str,
        expected_template: str,
        actual_template: str,
    ) -> None:
        self.location = location
        self.expected_template = expected_template
        self.actual_template = actual_template
        super().__init__(
            f"A thread at '{location}' expects labware of template "
            f"'{expected_template}', but got '{actual_template}'. Put the "
            f"expected template there, or fix the workflow."
        )


class TransitLabwareMissingDeckSiteError(RuntimeError):
    """Raised when a transit labware moves onto a multi-site deck without a valid site.

    On a multi-site device (a liquid handler with a derived deck) every transit
    labware needs its own named deck site, regardless of how many labware the
    action involves. A transit input the action moves in gets its site from the
    action's ``deck_positions``; the entry must resolve to a REAL deck site
    (derived from the deck layout) on that device. With no entry, or one that
    resolves to no site, the engine cannot choose where the plate lands and the
    action cannot address it. ``deck_positions`` values are BARE site names
    (``carrier-7-0``, ``C2-slot``); the engine prefixes the action's device, so
    a device-prefixed value double-prefixes and matches nothing. Residents are
    exempt: they are sited by their thread ``start=`` at a child site (the
    device-prefixed ``<device>/<site>`` form) and never move in.
    """

    def __init__(
        self,
        thread_name: str,
        device_location: str,
        template_name: str,
        available_sites: List[str],
        action_command: str,
        declared_site: Optional[str] = None,
    ) -> None:
        self.thread_name = thread_name
        self.device_location = device_location
        self.template_name = template_name
        self.available_sites = available_sites
        self.action_command = action_command
        self.declared_site = declared_site
        # A site id carries its device and deck_positions does not, so listing the
        # ids hands the operator the double-prefixed value the hint below warns about.
        prefix = f"{device_location}/"
        pasteable = [
            s[len(prefix):] if s.startswith(prefix) else s for s in available_sites
        ]
        sites = ", ".join(pasteable) if pasteable else "none"
        if declared_site is None:
            reason = (
                f"with no deck_positions entry. Every transit labware on a multi-site "
                f"deck needs its own named deck site; without one the engine cannot "
                f"choose where '{template_name}' lands. Add a deck_positions entry "
                f"mapping '{template_name}' to a deck site, on the @orca.action "
                f"for '{action_command}'."
            )
        else:
            looked_for = f"{device_location}/{declared_site}"
            hint = ""
            if declared_site.startswith(f"{device_location}/"):
                hint = (
                    f" deck_positions values are BARE site names (e.g. "
                    f"'carrier-7-0', 'C2-slot'); the action already targets "
                    f"'{device_location}', so the engine prefixes the device for "
                    f"you. '{declared_site}' is already device-prefixed, so it "
                    f"resolved to '{looked_for}' and matched no site. Drop the "
                    f"'{device_location}/' prefix. (The device-prefixed "
                    f"'<device>/<site>' form is only for thread start=/end=.)"
                )
            elif "/" in declared_site:
                hint = (
                    f" deck_positions values are BARE site names (e.g. "
                    f"'carrier-7-0', 'C2-slot') with no '/'; the action already "
                    f"targets '{device_location}', so the engine prefixes the "
                    f"device for you and '{declared_site}' resolved to "
                    f"'{looked_for}', matching no site. deck_positions cannot "
                    f"address another device's sites. (The device-prefixed "
                    f"'<device>/<site>' form is only for thread start=/end=.)"
                )
            reason = (
                f"via deck_positions site '{declared_site}' (the engine looked for "
                f"'{looked_for}'), which is not a deck site on that device. Map "
                f"'{template_name}' to a real deck site, on the @orca.action for "
                f"'{action_command}'.{hint}"
            )
        super().__init__(
            f"Thread '{thread_name}' moves labware '{template_name}' onto multi-site "
            f"device '{device_location}' {reason} Sites deck_positions accepts "
            f"here: {sites}."
        )


class SpawnContextUnavailableError(RuntimeError):
    """Raised when a reuse-bind dispatch fires but the ExecutingWorkflow was
    constructed without `labware_service` + `system` references.

    Three orca-core test fixtures construct ExecutingWorkflow without these
    refs (they predate the start-location checks); they keep working because they
    never use reuse-existing threads. Encountering this error means a
    reuse-existing thread is in play in a context that didn't plumb the
    refs through -- always a wiring bug, not an operator action.
    """


class AutoSpawnFailedError(RuntimeError):
    """An auto-spawn failed for a reason other than capacity. The thread pauses at SPAWN_CAPACITY, and RETRY spawns again."""


class CoLabwareLostError(RuntimeError):
    """An action's other labware will never arrive, so the waiting thread pauses."""
