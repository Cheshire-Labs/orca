from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Any, Awaitable, Callable, ClassVar, Dict, Protocol, Sequence, Tuple, cast
import uuid

from cheshire_drivers.labware_interfaces import IPlate, ITipRack, ITrough
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from cheshire_drivers.liquid_handler_models import TROUGH_WELL_ID, LabwareWellState
from orca.state.projections import (
    contents_provenance as fold_contents_provenance,
    has_tip_baseline,
    tips_present,
    went_unobserved as fold_went_unobserved,
)
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.state.identity import LabwareRef
from orca.state.provenance import Provenance
from orca.state.interfaces import IContentsReader, IContentsSeeder
from orca.state.records import (
    ObservationGapCause,
    DeclaredTracking,
    InitialStateDetails,
    LabwareInitialState,
    OperationRecord,
)
from orca.runtime.labware_catalog_protocol import (
    ILabwareCatalog,
    LabwareDefinition,
    LabwareNotFound,
)
from orca.runtime.labware_factory_resolver import resolve_plr_factory


orca_logger = logging.getLogger("orca")

CanContinueFn = Callable[["LabwareInstance", DeclaredTracking | None], Awaitable[bool]]


class ContentsUnbound(RuntimeError):
    """A labware was asked what it holds before anything bound it to the record.

    Always a wiring defect, never a state a run can be in: every route into the
    system seeds at birth and seeding binds.
    """


class TemplateCategoryMismatch(ValueError):
    """A labware template was constructed with a ``labware_type`` whose
    catalog category does not match the template class.

    Raised by ``LabwareTemplate.bind_catalog`` so the operator sees the
    misuse at System build time instead of as a cryptic adapter
    ``AttributeError`` (e.g. ``'PLRTroughAdapter' object has no attribute
    'barcode'``) deep inside workflow execution.
    """


# --- Template ABC (before instances so instances can reference it) ---

class LabwareTemplate(ABC):
    # Catalog category that this template's ``labware_type`` must satisfy.
    # ``None`` opts out of the category check (used by sim/wildcard templates
    # whose ``labware_type`` is synthetic and not catalog-registered).
    _expected_category: ClassVar[str | None] = None

    # Catalog-key string for the normal path; factory-typed templates may hold
    # a PLR factory callable at runtime. Set in each concrete subclass __init__.
    _labware_type: str

    def __init__(
        self,
        name: str,
        group_sharing: GroupSharing = GroupSharing.PER_GROUP,
        submission_batching: SubmissionBatching = SubmissionBatching.ISOLATED,
        can_continue_fn: CanContinueFn | None = None,
    ) -> None:
        self._name = name
        self._group_sharing = group_sharing
        self._submission_batching = submission_batching
        self._can_continue_fn = can_continue_fn
        self._catalog: ILabwareCatalog | None = None

    async def bind_catalog(self, catalog: ILabwareCatalog) -> None:
        """Inject the runtime labware catalog used by `create_instance()`.

        `SdkToSystemBuilder.bind_labwares` awaits this on every derived
        labware template once the System's catalog is fixed. Concrete
        subclasses that do not consult the catalog (sim stubs) override
        this to no-op.

        Also validates that the catalog row's ``category`` matches the
        template class's ``_expected_category``. Mismatches raise
        :class:`TemplateCategoryMismatch` so the operator sees the error
        at build time, not deep in execution.
        """
        self._catalog = catalog
        await self._validate_category()

    async def _validate_category(self) -> None:
        expected = self._expected_category
        if expected is None or self._catalog is None:
            return
        # ``_labware_type`` may be a PLR factory callable, not a catalog key;
        # only the string form has a catalog row to validate against.
        labware_type = getattr(self, "_labware_type", None)
        if not isinstance(labware_type, str):
            return
        try:
            definition = await self._catalog.get(labware_type)
        except (LabwareNotFound, KeyError):
            return
        actual = getattr(definition, "category", None)
        if not isinstance(actual, str) or actual == expected:
            return
        sibling = LabwareTemplate._find_template_class_for_category(actual)
        suggestion = (
            f" Use {sibling.__name__} instead of {type(self).__name__}."
            if sibling is not None
            else ""
        )
        raise TemplateCategoryMismatch(
            f"{type(self).__name__} {self._name!r} was given "
            f"labware_type={labware_type!r}, which is registered in the "
            f"catalog as category={actual!r} (expected {expected!r})."
            f"{suggestion}"
        )

    @classmethod
    def _find_template_class_for_category(
        cls, category: str,
    ) -> type["LabwareTemplate"] | None:
        """Locate the concrete ``LabwareTemplate`` subclass that handles a
        given catalog category. Used to suggest the right template class
        in :class:`TemplateCategoryMismatch` error messages.

        Walks direct subclasses of ``LabwareTemplate``; each concrete
        template owns its category via ``_expected_category``.
        """
        for sub in LabwareTemplate.__subclasses__():
            if sub._expected_category == category:
                return sub
        return None

    async def _plr_definition(self) -> LabwareDefinition:
        """The catalog row every PLR-backed template builds its labware from."""
        catalog = self._require_catalog()
        return await catalog.get(self._labware_type)

    def _require_catalog(self) -> ILabwareCatalog:
        if self._catalog is None:
            raise RuntimeError(
                f"LabwareTemplate {self._name!r}: no labware catalog is bound; "
                "SdkToSystemBuilder normally binds one when the System is "
                "constructed."
            )
        return self._catalog

    @property
    def name(self) -> str:
        return self._name

    @property
    def labware_type(self) -> str:
        """What this template was declared with, as a name.

        This is the identity every layer keyed by labware type looks up: grip
        profiles, per-position overrides, the catalog. It comes from the
        declaration rather than from anything the labware library carries,
        because a library's own name for a model is optional metadata and a key
        that is sometimes absent is not a key.

        A template declared with a factory callable answers with the factory's
        name, which is the same string a template declared by name would use.
        """
        declared = self._labware_type
        if isinstance(declared, str):
            return declared
        return getattr(declared, "__name__", str(declared))

    @property
    def group_sharing(self) -> GroupSharing:
        return self._group_sharing

    @property
    def submission_batching(self) -> SubmissionBatching:
        return self._submission_batching

    @property
    def can_continue_fn(self) -> CanContinueFn | None:
        return self._can_continue_fn

    @property
    def is_wildcard(self) -> bool:
        """True if this template matches any labware regardless of name.

        Concrete templates are name-typed (always False); see
        ``AnyLabwareTemplate.is_wildcard`` for the wildcard override.
        """
        return False

    def matches(self, labware_name: str) -> bool:
        """True if this template's labware identity matches ``labware_name``.

        Concrete templates match by name; wildcard templates override
        to match unconditionally. Comparing on ``.name`` rather than
        identity dodges the case where a workflow rebuild produces
        fresh template instances that wouldn't pass ``is``.
        """
        return self._name == labware_name

    @abstractmethod
    async def create_instance(self) -> "LabwareInstance":
        raise NotImplementedError("This method should be implemented by subclasses")

    async def restore_instance(self, persisted: "LabwareInstance") -> "LabwareInstance":
        """Rebuild a labware a store handed back into the instance the engine expects.

        A store keeps a labware's identity, never its PLR object, so a resident
        that outlives a restart comes back knowing only its name and template.
        Templates that own a PLR object rebuild one under the persisted name and
        id; templates that own none keep what the store gave them.
        """
        return persisted

    def _identity_only(self, instance: "LabwareInstance", skipped: str) -> bool:
        """True when a store handed back identity only, so there is no layout to read.

        Logs loudly and lets the caller skip its work. Better a deck the driver
        models from its own defaults than a build that wedges every route on the
        deployment; ``SystemRuntime`` rebuilds these at boot so reaching here at
        all means the rebuild could not run.
        """
        if instance.has_plr_backing:
            return False
        orca_logger.warning(
            "Labware %s came back from a store without its PLR object, so %s was "
            "skipped. Nothing rebuilt it from its template at boot, so its "
            "declared layout cannot be read and the driver keeps its own "
            "defaults for it.",
            instance.name, skipped,
        )
        return True

    def declared_contents(
        self, instance: "LabwareInstance",
    ) -> InitialStateDetails | None:
        """What this template says a fresh instance holds, or None for a
        template whose labware holds nothing trackable.

        This is the OPENING ledger entry and nothing else. It is written once,
        at birth, and never consulted again: a read that fell back to it would
        re-mint a consumed rack as full every time the record went quiet.

        Default: no contents. Subclasses describe their own (plate wells,
        trough pool, tip rack occupancy).
        """
        return None

    @property
    def declared_replenished(self) -> bool:
        """Sim-only: this labware is a reagent source that never runs dry.

        A property of the declaration rather than of the contents, so it rides
        the wire whatever the fold says.
        """
        return False


# --- Instances ---

def instance_name_for(template_name: str, instance_id: str) -> str:
    """The one place the ``<template>-<id prefix>`` name shape lives.

    The name doubles as the PLR object's name and the driver-world deck key,
    so persistence stores that re-derive it on rehydrate must use this too.
    8 hex chars: the name is a persisted key, so pair-collision odds must be
    negligible (4 chars = 1/65536 per same-template pair, too hot)."""
    return f"{template_name}-{instance_id[:8]}"


def mint_instance_identity(template_name: str) -> Tuple[str, str]:
    """Mint an (id, name) pair for a new instance."""
    instance_id = str(uuid.uuid4())
    return instance_id, instance_name_for(template_name, instance_id)


class LabwareInstance:
    """An instance of a labware that can be used in methods and workflows.

    Two identities are tracked here and they are not the same thing:

    * ``template_name`` -- stable, class-level string (e.g. ``"tips_384"``).
      Shared across every instance ever produced from the same template.
      Used by spawn routing, slot keys, feeder matching, well selectors,
      and ``DeclaredTracking`` dict keys (what the user writes).

    * ``name`` -- unique per physical instance
      (e.g. ``"tips_384-a1b2c3d4"``). The ops_history, ledger_projections,
      driver notifications, and error messages key off this. Two fresh
      racks from a stacker get different names so their ops buckets do
      not collide.
    """

    def __init__(
        self,
        template_name: str,
        labware_type: str,
        barcode: str | None = None,
        *,
        instance_id: str | None = None,
        name: str | None = None,
        has_plr_backing: bool = False,
        size_z: float | None = None,
    ) -> None:
        self._id = instance_id if instance_id is not None else str(uuid.uuid4())
        self._template_name = template_name
        self._name = name if name is not None else instance_name_for(template_name, self._id)
        self._labware_type = labware_type
        self._barcode = barcode
        self._template: LabwareTemplate | None = None
        self._metadata: Dict[str, str] = {}
        self._contents_reader: IContentsReader | None = None
        self._has_plr_backing = has_plr_backing
        self._size_z = size_z
        self._carry_override = MoveParameterPatch()

    @property
    def has_plr_backing(self) -> bool:
        """True when the live PLR object is attached to this instance.

        False for a labware a store handed back: it knows its name, template and
        barcode, and nothing that has to be read off the PLR object -- wells, tip
        spots, capacities. Anything reaching for geometry branches on this.
        """
        return self._has_plr_backing

    @property
    def barcode(self) -> str | None:
        return self._barcode

    @barcode.setter
    def barcode(self, value: str) -> None:
        self._barcode = value

    @property
    def metadata(self) -> Dict[str, str]:
        return self._metadata

    @property
    def size_z(self) -> float | None:
        """How tall this labware is, off the labware itself. None when unknown.

        An arm has to lift what it is holding clear of what held it, so the
        height belongs to the move. Read from the labware rather than looked up
        by name: a lookup can miss, and a missed height is a collision.
        """
        return self._size_z

    @property
    def carry_override(self) -> MoveParameterPatch:
        """How THIS piece of labware is being carried, over everything else.

        Empty for almost every labware: it is set when something true of this
        one object, and of nothing else, has to reach the arm. A lid fitted at
        the sealer, a plate filled to the brim that wants a slower move.
        """
        return self._carry_override

    def carry_with(self, **fields: float | str | None) -> None:
        """Carry this labware with these numbers until told otherwise.

        Merges, so naming the speed does not un-name the grip height stated
        earlier. Pass ``None`` for a field to hand it back to the layers
        underneath, which is the one thing setting a value cannot say.
        """
        patch = MoveParameterPatch.model_validate(
            {field: value for field, value in fields.items() if value is not None},
        )
        handing_back = [field for field, value in fields.items() if value is None]
        unknown = set(handing_back) - set(MoveParameterPatch.model_fields)
        if unknown:
            # A misspelt field would otherwise hand nothing back and report
            # success, which reads as an edit that worked.
            raise ValueError(f"not move parameters: {sorted(unknown)}")
        self._carry_override = patch.over(self._carry_override).without(
            cast(list[MoveParameterField], handing_back),
        )

    def carry_normally(self) -> None:
        """Drop the override; this labware goes back to resolving like any other."""
        self._carry_override = MoveParameterPatch()

    def restore_carry_override(self, patch: MoveParameterPatch) -> None:
        """Put back what a store kept. Replaces rather than merges: a restore
        reproduces a state, it does not add to one."""
        self._carry_override = patch

    @property
    def name(self) -> str:
        return self._name

    @property
    def template_name(self) -> str:
        return self._template_name

    @property
    def id(self) -> str:
        return self._id

    @property
    def labware_type(self) -> str:
        return self._labware_type

    @property
    def template(self) -> LabwareTemplate | None:
        return self._template

    async def enter_record(self, ledger: IContentsSeeder) -> None:
        """Bind this labware to the record and write its opening entry.

        The ONE route a labware enters the record by, whoever created it: a
        thread minting one, an operator registering one, a reuse-bound thread
        adopting a resident, a restart rehydrating one. One route rather than
        several is what makes a missed one loud -- an unbound labware raises on
        the first read instead of answering "nothing said", which the driver
        would fill with its own default.

        Binding first and unconditionally: being able to say what happened to a
        labware must not depend on having had something to declare.
        """
        self.bind_contents(ledger)
        template = self._template
        declared = template.declared_contents(self) if template is not None else None
        await ledger.seed_at_birth(self.ref, declared)

    @property
    def ref(self) -> LabwareRef:
        """How the record names this labware: its id, and the name a record
        spells it with."""
        return LabwareRef(id=self._id, name=self.name)

    def bind_contents(self, reader: IContentsReader) -> None:
        """Bind the one thing that can say what happened to this labware.

        The registry binds it as the labware enters the system. Rebinding is
        fine: every reader answers from the same store, so which one is held
        makes no difference to the answer.
        """
        self._contents_reader = reader

    def well_capacity(self, well_id: str) -> float | None:
        """Max volume the named well can hold, or None when unknown/unbounded.

        Overridden by volume-bearing instances (plate, trough). Used by the
        operator set-volume operation to reject overfill; None means no
        capacity bound is known so the operation does not clamp."""
        return None

    async def ops(self) -> list[OperationRecord]:
        """Every op the record holds for this instance, chronologically.

        Reads the store, not a per-execution binding: a restart severs bindings
        and the contents of a rack must not depend on whether one survived.
        Empty for an instance no registry has bound a reader to.
        """
        if self._contents_reader is None:
            return []
        return await self._contents_reader.ops_of(self.ref)

    async def note_observation_gap(self, cause: ObservationGapCause) -> None:
        """Record that nobody was watching this labware for a while.

        Silent when nothing is bound, unlike a read: a gap is a courtesy to a
        later reader, and refusing to pause a thread because a labware was not
        wired up would trade a recoverable stop for an unrecoverable one.
        """
        if self._contents_reader is None:
            return
        await self._contents_reader.note_observation_gap(self.ref, cause)

    async def driver_well_state(self) -> LabwareWellState | None:
        """What this labware holds, in the shape its driver takes.

        None when the record has said nothing: the driver then keeps its own
        defaults, which is the honest answer to a question nobody has answered.

        An unbound labware RAISES rather than answering None. The two are not the
        same thing and the difference is the whole point: "nobody has said what is
        in it" is an answer, while "nothing wired this labware to the record" is a
        bug, and returning None for both hands the driver its own default -- which
        for a tip rack is a full rack, the exact failure this design exists to
        stop. A miss should be loud at the seam, not silently wrong on the wire.
        """
        if self._contents_reader is None:
            raise ContentsUnbound(
                f"labware {self.name!r} ({self.id}) was never bound to the "
                f"contents record, so what it holds cannot be read. Every route "
                f"into the system seeds at birth, which binds; reaching this "
                f"means one route does not."
            )
        tips, volumes = await self._contents_reader.wire_contents(self.ref)
        return self.contents_wire_state(tips=tips, volumes=volumes)

    def contents_wire_state(
        self, tips: dict[str, bool] | None, volumes: dict[str, float] | None,
    ) -> LabwareWellState | None:
        """This labware's folded contents in the shape its driver takes.

        The fold is the same for every labware; which half of it means anything
        is not. A rack takes tips, a plate and a trough take volumes, and a
        labware that holds neither takes nothing.
        """
        del tips, volumes
        return None

    async def contents_provenance(self) -> Provenance:
        """How well the record knows what this labware holds right now.

        STALE means one of four things: a restart, a reconnect or an error pause
        opened a stretch nobody watched; an operator command only made sense if
        the record was wrong; an aborted action threw away work it had really
        done; or an unfinished action is holding operations the record has not
        been told about yet. Either way the fold describes then rather than
        now. A caller about to refuse on the fold needs that: refusing on a
        reading nobody has questioned is a different act from refusing on one
        nobody has looked at. Which of the four it is matters to a gate that
        lets work through -- see ``went_unobserved``.

        Graded by the ledger so this and every operator surface answer the
        same. Unbound labware has no ledger to ask and folds on its own.
        """
        ops = await self.ops()
        if self._contents_reader is None:
            return fold_contents_provenance(ops, self.name)
        return self._contents_reader.provenance_of(ops, self.name)

    async def went_unobserved(self) -> bool:
        """True when a stretch went by with nobody watching this labware.

        Narrower than ``contents_provenance() is STALE``, and the two are not
        interchangeable for a gate that lets work through. A read is stale for
        several reasons; only an unwatched stretch means a hand could have
        refilled the rack, which is the one reason to let a pick proceed at a
        position the record calls empty. An unfinished action holding
        operations, or an abandoned one that dropped them, is not that: nobody
        was at the deck, so the gate must still refuse.

        Graded by the ledger, like ``contents_provenance``, because an action
        holding its operations is something only the ledger can see. Unbound
        labware has no ledger and folds on its own.
        """
        ops = await self.ops()
        if self._contents_reader is None:
            return fold_went_unobserved(ops, self.name)
        return self._contents_reader.went_unobserved(ops, self.name)

    async def missing_tip_positions(self, positions: Sequence[str]) -> list[str]:
        """Requested tip positions the ledger does not back, or none: not a
        tip rack. Overridden by ``TipRackInstance``."""
        del positions
        return []

    def every_tip_position(self) -> list[str]:
        """Every position this labware has a tip spot at, or none: not a tip
        rack. Overridden by ``TipRackInstance``."""
        return []

    async def tip_count_present(self) -> int:
        """Tips the ledger currently backs, or 0: not a tip rack. Overridden
        by ``TipRackInstance``; only meaningful after ``missing_tip_positions``
        reports a shortfall, so an operator can tell "reload the rack" from
        "advance to the next column"."""
        return 0

    def _declared_replenished(self) -> bool:
        return self._template is not None and self._template.declared_replenished

    async def can_continue(self, demand: DeclaredTracking | None = None) -> bool:
        """Async accessor: True if the labware can keep participating, False to stop.

        One of two public async members on LabwareInstance (with ``ops``), so it
        MUST be awaited; an un-awaited call returns a truthy coroutine and silently
        disables the stop check. True = keep going; False = stop. Template-level
        can_continue_fn wins if registered; otherwise the subclass default runs.
        Only TipRackInstance overrides the default (tips still present); plates
        and everything else inherit the unbounded base True, so capacity for
        those must come from a can_continue_fn or a slot CapacityPolicy.
        """
        if self._template is not None and self._template.can_continue_fn is not None:
            return await self._template.can_continue_fn(self, demand)
        return await self._can_continue_default(demand)

    async def _can_continue_default(self, demand: DeclaredTracking | None = None) -> bool:
        return True

    def __str__(self) -> str:
        return self._name


class PlateInstance(LabwareInstance):
    """A class that represents a plate instance.

    The PLR object arrives already carrying the minted INSTANCE name (the
    driver-world deck key); ``template_name`` is passed explicitly rather than
    read off the PLR object."""

    def __init__(
        self,
        labware: IPlate,
        *,
        template_name: str,
        labware_type: str,
        instance_id: str | None = None,
    ) -> None:
        super().__init__(
            template_name, labware_type, barcode=labware.barcode,
            instance_id=instance_id, name=labware.name, has_plr_backing=True,
            size_z=labware.size_z,
        )
        self._plate = labware

    @property
    def plate(self) -> IPlate:
        """Returns the underlying plate object."""
        return self._plate

    def contents_wire_state(
        self, tips: dict[str, bool] | None, volumes: dict[str, float] | None,
    ) -> LabwareWellState | None:
        del tips
        if volumes is None:
            return None
        return LabwareWellState(
            volumes=volumes, replenished=self._declared_replenished(),
        )

    def well_capacity(self, well_id: str) -> float | None:
        try:
            return self._plate.well(well_id).max_volume
        except (KeyError, ValueError):
            return None

    @property
    def has_lid(self) -> bool:
        """Whether this plate currently carries a lid.

        Mirrors PLR's `Plate.has_lid()` via the IPlate adapter chain.
        Workflows branching on lid presence should prefer this over
        reaching through `instance.plate.has_lid` for ergonomics.
        """
        return self._plate.has_lid


class TubeRackInstance(LabwareInstance):
    """A class that represents a tube rack instance"""

    def __init__(
        self,
        labware: Any,
        *,
        template_name: str,
        labware_type: str,
        instance_id: str | None = None,
    ) -> None:
        name = labware.name if hasattr(labware, "name") else str(labware)
        super().__init__(
            template_name, labware_type, instance_id=instance_id, name=name,
            has_plr_backing=True,
            size_z=labware.size_z if hasattr(labware, "size_z") else None,
        )
        self._tube_rack = labware


class TipRackInstance(LabwareInstance):
    """A class that represents a tip rack instance"""

    def __init__(
        self,
        labware: ITipRack,
        *,
        template_name: str,
        labware_type: str,
        instance_id: str | None = None,
    ) -> None:
        super().__init__(
            template_name, labware_type,
            instance_id=instance_id, name=labware.name, has_plr_backing=True,
            size_z=labware.size_z,
        )
        self._tip_rack = labware

    @property
    def tip_rack(self) -> ITipRack:
        """Returns the underlying tip rack object."""
        return self._tip_rack

    def contents_wire_state(
        self, tips: dict[str, bool] | None, volumes: dict[str, float] | None,
    ) -> LabwareWellState | None:
        del volumes
        if tips is None:
            return None
        # Every position the rack HAS rides the wire, not only the ones the
        # record has mentioned. Which positions exist is geometry, not a claim
        # about contents; leaving one out is what lets a driver fill the silence
        # with its own factory default, and that default is a full rack.
        spots = {spot.identifier for spot in self._tip_rack.tip_spots()}
        return LabwareWellState(
            tips={pos: tips.get(pos, False) for pos in spots | set(tips)},
        )

    async def next_tips(self, count: int) -> list[str]:
        """The next ``count`` positions on this rack that still hold a tip.

        Ask for tips rather than naming a column: a rack that survives between
        runs has no full column to name by the second one.
        """
        if self._contents_reader is None:
            raise ContentsUnbound(
                f"tip rack {self.name!r} ({self.id}) was never bound to the "
                f"contents record, so its next tips cannot be chosen. Every "
                f"route into the system seeds at birth, which binds; reaching "
                f"this means one route does not."
            )
        return await self._contents_reader.next_available_tips(self, count)

    def every_tip_position(self) -> list[str]:
        """Which positions exist on this rack. Geometry, not a claim about
        contents: a 96-head draws from all of them at once, so a caller
        checking that pick has to name them all."""
        return [spot.identifier for spot in self._tip_rack.tip_spots()]

    async def missing_tip_positions(self, positions: Sequence[str]) -> list[str]:
        """Requested positions this rack's ledger does not currently hold a
        tip at, checked before a pick dispatches to hardware.

        Empty when the ledger has no baseline for this rack: nothing to check
        against means nothing blocks the pick, the same default
        ``_can_continue_default`` uses -- a rack this route never bound reads
        as "nothing said" here too.
        """
        ops = await self.ops()
        if not has_tip_baseline(ops, self.name):
            return []
        present = tips_present(ops, self.name)
        return [pos for pos in positions if pos not in present]

    async def tip_count_present(self) -> int:
        ops = await self.ops()
        if not has_tip_baseline(ops, self.name):
            return 0
        return len(tips_present(ops, self.name))

    async def _can_continue_default(self, demand: DeclaredTracking | None = None) -> bool:
        ops = await self.ops()
        if not has_tip_baseline(ops, self.name):
            # Nothing bound describes where this rack started, so the fold has
            # no baseline and cannot tell "ran out" from "never seen".
            return True
        present = tips_present(ops, self.name)
        if demand is not None and demand.tips_used and self.template_name in demand.tips_used:
            return all(pos in present for pos in demand.tips_used[self.template_name])
        return len(present) > 0


class TroughInstance(LabwareInstance):
    """A class that represents a trough instance"""

    def __init__(
        self,
        labware: ITrough,
        *,
        template_name: str,
        labware_type: str,
        instance_id: str | None = None,
    ) -> None:
        super().__init__(
            template_name, labware_type,
            instance_id=instance_id, name=labware.name, has_plr_backing=True,
            size_z=labware.size_z,
        )
        self._trough = labware

    @property
    def trough(self) -> ITrough:
        """Returns the underlying trough object."""
        return self._trough

    def contents_wire_state(
        self, tips: dict[str, bool] | None, volumes: dict[str, float] | None,
    ) -> LabwareWellState | None:
        del tips
        if volumes is None:
            return None
        return LabwareWellState(
            volumes=volumes, replenished=self._declared_replenished(),
        )

    def well_capacity(self, well_id: str) -> float | None:
        # Single pool: every well id maps to the one reservoir capacity.
        return self._trough.max_volume


class AnyLabware:
    """An instance of AnyLabwareTemplate that can be used in methods that accept any labware type."""

    @property
    def name(self) -> str:
        return "$any"

    def __str__(self) -> str:
        return "$AnyLabware"


# --- Concrete Templates ---

def _carry_persisted_identity(
    persisted: LabwareInstance, rebuilt: LabwareInstance,
) -> None:
    """Move what a store kept but a fresh PLR object cannot know onto the rebuild.

    Name and id already match: the rebuild is built under the persisted ones.
    """
    if persisted.barcode is not None:
        rebuilt.barcode = persisted.barcode
    rebuilt.metadata.update(persisted.metadata)
    # A rebuild that dropped this would carry a lidded plate as a bare one on
    # the very next move, with nothing in the log to say why it changed.
    rebuilt.restore_carry_override(persisted.carry_override)


def _enumerate_plate_well_ids(plate: IPlate) -> list[str]:
    """A1, A2, ..., H12 style IDs derived from num_rows x num_cols."""
    ids: list[str] = []
    for r in range(plate.num_rows):
        row_letter = chr(ord("A") + r)
        for c in range(plate.num_cols):
            ids.append(f"{row_letter}{c + 1}")
    return ids


def _resolve_plate_initial_volumes(
    plate: IPlate, spec: LabwareInitialState
) -> dict[str, float]:
    well_ids = _enumerate_plate_well_ids(plate)
    if spec.max_fill:
        return {w: plate.well(w).max_volume for w in well_ids}
    if spec.wells is not None:
        result = {w: 0.0 for w in well_ids}
        result.update(spec.wells)
        return result
    if spec.uniform_volume is not None:
        return {w: spec.uniform_volume for w in well_ids}
    return {w: 0.0 for w in well_ids}


class PlateTemplate(LabwareTemplate):
    """A class that represents a plate template"""

    _expected_category: ClassVar[str | None] = "plate"

    def __init__(
        self,
        name: str,
        labware_type: str,
        with_lid: bool = False,
        group_sharing: GroupSharing = GroupSharing.PER_GROUP,
        submission_batching: SubmissionBatching = SubmissionBatching.ISOLATED,
        can_continue_fn: CanContinueFn | None = None,
        initial_state: LabwareInitialState | None = None,
    ) -> None:
        super().__init__(
            name,
            group_sharing=group_sharing,
            submission_batching=submission_batching,
            can_continue_fn=can_continue_fn,
        )
        self._with_lid = with_lid
        self._labware_type = labware_type
        self._initial_state = initial_state

    async def _build_instance(self, instance_id: str, instance_name: str) -> PlateInstance:
        factory: Callable[..., IPlate] = resolve_plr_factory(await self._plr_definition())
        try:
            plate = factory(instance_name, self._with_lid if self._with_lid else None)
        except TypeError:
            if self._with_lid:
                raise ValueError(
                    f"Plate type {self._labware_type!r} does not support 'with_lid'"
                )
            plate = factory(instance_name)
        instance = PlateInstance(
            plate,
            template_name=self.name,
            labware_type=self.labware_type,
            instance_id=instance_id,
        )
        instance._template = self
        return instance

    async def create_instance(self) -> PlateInstance:
        instance_id, instance_name = mint_instance_identity(self.name)
        return await self._build_instance(instance_id, instance_name)

    async def restore_instance(self, persisted: LabwareInstance) -> PlateInstance:
        rebuilt = await self._build_instance(persisted.id, persisted.name)
        _carry_persisted_identity(persisted, rebuilt)
        return rebuilt

    def declared_contents(self, instance: LabwareInstance) -> InitialStateDetails | None:
        if self._identity_only(instance, "its opening ledger entry"):
            return None
        assert isinstance(instance, PlateInstance), "PlateTemplate declares PlateInstance only"
        if self._initial_state is None:
            # Nobody said what this plate holds, so the record says nothing and
            # a read answers "unknown". Writing a grid of zeros here would
            # instead claim every well was seen empty.
            return None
        return InitialStateDetails(
            labware=instance.name,
            well_volumes=_resolve_plate_initial_volumes(
                instance.plate, self._initial_state,
            ),
        )

    @property
    def declared_replenished(self) -> bool:
        return self._initial_state is not None and self._initial_state.replenished


class TubeRackTemplate(LabwareTemplate):
    """A class that represents a tube rack template"""

    _expected_category: ClassVar[str | None] = "tube"

    def __init__(
        self,
        name: str,
        labware_type: str,
        can_continue_fn: CanContinueFn | None = None,
    ) -> None:
        super().__init__(name, can_continue_fn=can_continue_fn)
        self._labware_type = labware_type

    async def _build_instance(self, instance_id: str, instance_name: str) -> TubeRackInstance:
        factory = resolve_plr_factory(await self._plr_definition())
        tube_rack = factory(instance_name)
        instance = TubeRackInstance(
            tube_rack,
            template_name=self.name,
            labware_type=self.labware_type,
            instance_id=instance_id,
        )
        instance._template = self
        return instance

    async def create_instance(self) -> TubeRackInstance:
        instance_id, instance_name = mint_instance_identity(self.name)
        return await self._build_instance(instance_id, instance_name)

    async def restore_instance(self, persisted: LabwareInstance) -> TubeRackInstance:
        rebuilt = await self._build_instance(persisted.id, persisted.name)
        _carry_persisted_identity(persisted, rebuilt)
        return rebuilt

    # A tube rack tracks no contents; the base declaration (none) stands.


class TipRackTemplate(LabwareTemplate):
    """A class that represents a tip rack template"""

    _expected_category: ClassVar[str | None] = "tip_rack"

    def __init__(
        self,
        name: str,
        labware_type: str,
        with_tips: bool,
        group_sharing: GroupSharing = GroupSharing.PER_GROUP,
        submission_batching: SubmissionBatching = SubmissionBatching.ISOLATED,
        can_continue_fn: CanContinueFn | None = None,
        initial_state: LabwareInitialState | None = None,
    ) -> None:
        super().__init__(
            name,
            group_sharing=group_sharing,
            submission_batching=submission_batching,
            can_continue_fn=can_continue_fn,
        )
        self._with_tips = with_tips
        self._labware_type = labware_type
        self._initial_state = initial_state

    async def _build_instance(self, instance_id: str, instance_name: str) -> TipRackInstance:
        factory: Callable[..., ITipRack] = resolve_plr_factory(await self._plr_definition())
        tip_rack = factory(instance_name, self._with_tips)
        instance = TipRackInstance(
            tip_rack,
            template_name=self.name,
            labware_type=self.labware_type,
            instance_id=instance_id,
        )
        instance._template = self
        return instance

    async def create_instance(self) -> TipRackInstance:
        instance_id, instance_name = mint_instance_identity(self.name)
        return await self._build_instance(instance_id, instance_name)

    async def restore_instance(self, persisted: LabwareInstance) -> TipRackInstance:
        rebuilt = await self._build_instance(persisted.id, persisted.name)
        _carry_persisted_identity(persisted, rebuilt)
        return rebuilt

    def declared_contents(self, instance: LabwareInstance) -> InitialStateDetails | None:
        if self._identity_only(instance, "its opening ledger entry"):
            return None
        assert isinstance(instance, TipRackInstance), "TipRackTemplate declares TipRackInstance only"
        spec = self._initial_state
        if spec is not None and spec.tip_positions is not None:
            positions = list(spec.tip_positions)
        elif spec is not None and spec.max_fill:
            positions = [spot.identifier for spot in instance.tip_rack.tip_spots()]
        elif self._with_tips:
            positions = [spot.identifier for spot in instance.tip_rack.tip_spots()]
        else:
            positions = []
        return InitialStateDetails(
            labware=instance.name, tip_positions_present=positions,
        )


class TroughTemplate(LabwareTemplate):
    """A class that represents a trough template"""

    _expected_category: ClassVar[str | None] = "trough"

    def __init__(
        self,
        name: str,
        labware_type: str,
        group_sharing: GroupSharing = GroupSharing.PER_GROUP,
        submission_batching: SubmissionBatching = SubmissionBatching.ISOLATED,
        can_continue_fn: CanContinueFn | None = None,
        initial_state: LabwareInitialState | None = None,
    ) -> None:
        super().__init__(
            name,
            group_sharing=group_sharing,
            submission_batching=submission_batching,
            can_continue_fn=can_continue_fn,
        )
        self._labware_type = labware_type
        self._initial_state = initial_state

    async def _build_instance(self, instance_id: str, instance_name: str) -> TroughInstance:
        factory: Callable[..., ITrough] = resolve_plr_factory(await self._plr_definition())
        trough = factory(instance_name)
        instance = TroughInstance(
            trough,
            template_name=self.name,
            labware_type=self.labware_type,
            instance_id=instance_id,
        )
        instance._template = self
        return instance

    async def create_instance(self) -> TroughInstance:
        instance_id, instance_name = mint_instance_identity(self.name)
        return await self._build_instance(instance_id, instance_name)

    async def restore_instance(self, persisted: LabwareInstance) -> TroughInstance:
        rebuilt = await self._build_instance(persisted.id, persisted.name)
        _carry_persisted_identity(persisted, rebuilt)
        return rebuilt

    def declared_contents(self, instance: LabwareInstance) -> InitialStateDetails | None:
        if self._identity_only(instance, "its opening ledger entry"):
            return None
        assert isinstance(instance, TroughInstance), "TroughTemplate declares TroughInstance only"
        spec = self._initial_state
        if spec is not None and spec.max_fill:
            volumes = {TROUGH_WELL_ID: instance.trough.max_volume}
        elif spec is not None and spec.wells is not None:
            volumes = dict(spec.wells)
        elif spec is not None and spec.uniform_volume is not None:
            volumes = {TROUGH_WELL_ID: spec.uniform_volume}
        else:
            # An undeclared trough still writes an opening entry, unlike a
            # plate: the fold reads `single_pool` off it to know a 96-head
            # aspirate takes 96 channels out of one reservoir, not one.
            volumes = {TROUGH_WELL_ID: 0.0}
        return InitialStateDetails(
            labware=instance.name, well_volumes=volumes, single_pool=True,
        )

    @property
    def declared_replenished(self) -> bool:
        return self._initial_state is not None and self._initial_state.replenished


class AnyLabwareTemplate:
    """Acts as a placeholder for any labware type, allowing for flexible methods that can accept any labware."""

    @property
    def name(self) -> str:
        return "$any"

    @property
    def is_wildcard(self) -> bool:
        return True

    def matches(self, labware_name: str) -> bool:
        return True

    async def create_instance(self) -> AnyLabware:
        return AnyLabware()

    def __str__(self) -> str:
        return "$AnyLabwareTemplate"
