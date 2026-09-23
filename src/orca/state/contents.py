"""What a labware holds: one owner, one read path, one write path.

Precedence, highest first:

1. an operator assertion (set / confirm)
2. the ledger fold of the operations the engine commanded
3. the template declaration -- which is only ever the OPENING entry, written
   once at birth, and never a read-time fallback

A driver's own report sits outside that list. It is a witness: its numbers came
from what orca projected onto that driver, so folding it would launder our own
guess back in as evidence. It is compared instead, and a disagreement is raised
for an operator to settle.

Reads go to the store rather than to buckets bound on the instance, because a
restart severs bindings and the whole point is that a restart changes nothing.
"""

from dataclasses import dataclass
import logging

from orca.state.identity import LabwareRef
from orca.state.projections import (
    Provenance,
    Source,
    contents_provenance,
    contents_source,
    has_contents_baseline,
    has_tip_baseline,
    has_volume_history,
    latest_driver_report,
    sparse_volumes,
    went_unobserved as fold_went_unobserved,
    tip_layout,
    tip_positions_seen,
    tips_present,
    well_volumes,
)
from orca.state.ops_history import OpsHistory
from orca.state.records import (
    InitialStateDetails,
    ObservationGapCause,
    OperationRecord,
)
from orca.state.ops_store import ops_for_labware


orca_logger = logging.getLogger("orca")


@dataclass(frozen=True)
class LabwareContents:
    """What one labware holds, and how well the record knows it.

    ``volumes`` and ``tips`` cover only what the record has spoken about; a
    labware whose provenance is UNKNOWN has both empty, and that emptiness means
    "never told", not "empty labware".
    """

    provenance: Provenance
    volumes: dict[str, float]
    tips: dict[str, bool]

    @property
    def is_known(self) -> bool:
        return self.provenance is not Provenance.UNKNOWN

    @property
    def tip_positions_present(self) -> list[str]:
        return sorted(pos for pos, held in self.tips.items() if held)


@dataclass(frozen=True)
class ContentsDivergence:
    """A driver and the ledger describing the same labware differently.

    Neither side can settle it alone: the driver may have come up on defaults
    after a reconnect, and the ledger cannot see a hand. An operator picks the
    side with set-tip-state / set-well-volumes.
    """

    labware_id: str
    labware_name: str
    ledger_tip_positions: list[str]
    driver_tip_positions: list[str]

    def describe(self) -> str:
        return (
            f"{self.labware_name}: the record has {len(self.ledger_tip_positions)} "
            f"tips, the driver reports {len(self.driver_tip_positions)}"
        )


@dataclass(frozen=True)
class ContentsLayerReading:
    """What one layer says, and whether it matches the resolved answer.

    Layers are not peers. They are listed so a reader can see what was shadowed
    and stop shopping between endpoints for a better-looking number.
    """

    layer: str
    tip_count: int | None
    agrees: bool
    note: str


@dataclass(frozen=True)
class ContentsResolution:
    """The one answer to "what does this labware hold", plus its receipts.

    Modelled on the variable-resolution read: the resolved value, the layer it
    came from, and every layer that was shadowed. ``get-tip-state`` and a
    driver's own deck state stay available as the raw layers feeding this, which
    is what they are -- diagnostics, not rival answers.
    """

    labware_id: str
    labware_name: str
    provenance: Provenance
    source: Source
    tip_positions_present: list[str] | None
    tip_count: int | None
    volumes: dict[str, float] | None
    layers: list[ContentsLayerReading]


class ContentsUnknown(RuntimeError):
    """Asked what a labware holds when nothing has ever said."""


class NotEnoughTips(RuntimeError):
    """A rack was asked for more tips than it holds."""


def _column_major(position: str) -> tuple[int, str]:
    """Sort key putting A1, B1, ... H1 before A2: the order a head takes them."""
    row = position[:1]
    column = position[1:]
    return (int(column) if column.isdigit() else 0, row)


class LabwareContentsLedger:
    """Reads and writes what a labware holds. The only place that decides.

    Holds no state of its own: every answer is folded from the ops store on
    demand, so two callers cannot drift and a restart cannot lose anything the
    store kept.
    """

    def __init__(self, ops_history: OpsHistory) -> None:
        self._ops_history = ops_history

    async def ops_of(self, labware: LabwareRef) -> list[OperationRecord]:
        """Every op touching this instance, across every execution bucket."""
        return await ops_for_labware(
            self._ops_history.store, labware.name, labware.id,
        )

    async def of(self, labware: LabwareRef) -> LabwareContents:
        return self._fold(await self.ops_of(labware), labware.name)

    def provenance_of(
        self, ops: list[OperationRecord], labware_name: str,
    ) -> Provenance:
        """How well the record knows this labware, over ops already read.

        Taken as a second entry point by callers that hold the ops already, so
        the fold and the worklist cannot end up disagreeing about the same
        labware. An action that has aspirated and not yet finished is holding
        those records, which leaves this read behind: the numbers stay what the
        record says, because an unfinished action may still be retried and
        counting its operations twice is a different wrong answer, but the read
        stops calling itself known.
        """
        provenance = contents_provenance(ops, labware_name)
        if (
            provenance is Provenance.KNOWN
            and self._ops_history.unrecorded.touches_labware(labware_name)
        ):
            return Provenance.STALE
        return provenance

    def went_unobserved(
        self, ops: list[OperationRecord], labware_name: str,
    ) -> bool:
        """Whether a gate may let work through at a position this reads empty.

        The fold answers for the gaps it can see. An action still holding its
        operations is not one of them, and it means the record is behind by
        picks that really happened, so a rack the fold would have called
        reloadable is refused while that action is unsettled.
        """
        if self._ops_history.unrecorded.touches_labware(labware_name):
            return False
        return fold_went_unobserved(ops, labware_name)

    def is_behind(self, labware_name: str) -> bool:
        """Whether an unfinished action is holding operations on this labware.

        Different from every other stale: the record is not wrong, it has
        simply not been told yet, and settling the action tells it.
        """
        return self._ops_history.unrecorded.touches_labware(labware_name)

    def _fold(self, ops: list[OperationRecord], name: str) -> LabwareContents:
        """What the record has actually spoken about, and how well it knows it.

        Every well the record has an entry for, INCLUDING the ones a run drained
        to exactly zero. Absent means nobody has said; zero means known to be
        empty, and a plate the run emptied is the second. The driver's
        projection drops those zeros on purpose -- silence is what keeps its
        tracker lenient about a well nobody described -- but an operator reading
        a plate they just emptied must not be shown a blank.

        A labware the record has never spoken about has no entries at all, so
        the map is empty and the provenance says unknown rather than the map
        pretending to an answer.
        """
        tips: dict[str, bool] = {}
        if has_tip_baseline(ops, name):
            present = tips_present(ops, name)
            tips = {pos: pos in present for pos in tip_positions_seen(ops, name)}
        volumes = well_volumes(ops, name) if has_volume_history(ops, name) else {}
        return LabwareContents(
            provenance=self.provenance_of(ops, name),
            volumes=volumes,
            tips=tips,
        )

    # -- Birth ---------------------------------------------------------------

    async def seed_at_birth(
        self, labware: LabwareRef, declared: InitialStateDetails | None,
    ) -> None:
        """Write the declared contents, once, if nothing has yet.

        Every route a labware can enter the system by calls this: a thread
        minting one, an operator registering one, a reuse-bound thread adopting
        a resident, a restart rehydrating one. Guarded on the record rather than
        on the route, so a restart re-running it changes nothing -- which is
        what stops a consumed rack coming back full.

        The caller resolves the declaration from the template, because a
        template is topology and the ledger does not take topology. What arrives
        here is a value, and it is the opening entry and nothing else: no read
        ever consults it again.
        """
        if declared is None:
            return
        if has_contents_baseline(await self.ops_of(labware), labware.name):
            return
        await self._ops_history.append_initial_state(
            labware.name, declared, labware.id,
        )

    # -- Operator assertions -------------------------------------------------

    async def assert_tips(
        self, labware: LabwareRef, positions: list[str],
    ) -> None:
        """Record that an operator says the rack holds exactly these positions."""
        await self._ops_history.append_set_tip_state(
            labware.name, list(positions), labware.id,
        )

    async def assert_volumes(
        self, labware: LabwareRef, volumes: dict[str, float],
    ) -> None:
        """Record that an operator says these wells hold exactly these amounts."""
        await self._ops_history.append_set_volume(
            labware.name, dict(volumes), labware.id,
        )

    async def mark_tips_used(
        self, labware: LabwareRef, positions: list[str],
    ) -> list[str]:
        """Record that these positions no longer hold a tip. Returns what is left.

        The ordinary repair when the rack is emptier than the record thinks: a
        pick found air, or a hand took a column. Absolute like every operator
        assertion, but stated as a subtraction so nobody has to retype the
        ninety positions that did not change.
        """
        contents = await self.of(labware)
        if not contents.is_known:
            raise ContentsUnknown(
                f"nothing has ever said what {labware.name!r} holds, so tips "
                f"cannot be marked used. State the layout with set-tip-state."
            )
        unknown = [p for p in positions if p not in contents.tips]
        if unknown:
            raise ContentsUnknown(
                f"{labware.name!r} has no positions {unknown}. A typo that "
                f"changed nothing used to answer success, leaving the record "
                f"unrepaired and the next pick aimed at an empty spot."
            )
        remaining = [p for p in contents.tip_positions_present if p not in set(positions)]
        await self.assert_tips(labware, remaining)
        return remaining

    async def note_observation_gap(
        self, labware: LabwareRef, cause: ObservationGapCause,
    ) -> None:
        """Record that nobody was watching this labware for a while.

        Moves nothing. It only expires an earlier attestation, so the read stops
        claiming a human has looked since. Skipped for a labware the record knows
        nothing about: there is no attestation to expire.
        """
        if not has_contents_baseline(await self.ops_of(labware), labware.name):
            return
        await self._ops_history.append_observation_gap(
            labware.name, cause, labware.id,
        )

    # -- Driver projection ---------------------------------------------------

    async def wire_contents(
        self, labware: LabwareRef,
    ) -> tuple[dict[str, bool] | None, dict[str, float] | None]:
        """What the record has spoken about, as ``(tips, volumes)``.

        Sparse on purpose: only what has been spoken about rides the wire, so an
        undeclared labware keeps the driver's lenient tracker. A well drained to
        exactly zero, or a position whose tip was picked, rides it explicitly
        rather than dropping off -- otherwise the driver fills the silence with
        its own factory default, which for a tip rack is a full rack.
        """
        ops = await self.ops_of(labware)
        return tip_layout(ops, labware.name), sparse_volumes(ops, labware.name)

    # -- The canonical read --------------------------------------------------

    async def resolve(
        self, labware: LabwareRef, declared_tip_count: int | None = None,
    ) -> ContentsResolution:
        """The single answer, with every layer's reading attached.

        One call, so nothing downstream has to compare four endpoints and guess
        which to believe.
        """
        ops = await self.ops_of(labware)
        contents = self._fold(ops, labware.name)
        holds_tips = bool(contents.tips) or has_tip_baseline(ops, labware.name)
        present = contents.tip_positions_present if holds_tips else None
        layers = [
            ContentsLayerReading(
                layer="ledger",
                tip_count=len(present) if present is not None else None,
                agrees=True,
                note="the record of what was commanded, plus anything an operator stated",
            ),
        ]
        reported = latest_driver_report(ops, labware.name)
        if reported is not None and reported.tip_positions_present is not None:
            driver_count = len(reported.tip_positions_present)
            layers.append(ContentsLayerReading(
                layer="driver",
                tip_count=driver_count,
                agrees=present is not None and driver_count == len(present),
                note="what the driver last reported; it holds what orca projected onto it",
            ))
        if declared_tip_count is not None:
            layers.append(ContentsLayerReading(
                layer="declaration",
                tip_count=declared_tip_count,
                agrees=present is not None and declared_tip_count == len(present),
                note="the template's opening entry; never an answer about now",
            ))
        return ContentsResolution(
            labware_id=labware.id,
            labware_name=labware.name,
            provenance=contents.provenance,
            source=contents_source(ops, labware.name),
            tip_positions_present=present,
            tip_count=len(present) if present is not None else None,
            volumes=contents.volumes if contents.volumes else None,
            layers=layers,
        )

    # -- Allocation ----------------------------------------------------------

    async def next_available_tips(
        self, labware: LabwareRef, count: int,
    ) -> list[str]:
        """The next ``count`` positions on this rack that still hold a tip.

        Column-major, the order an 8-channel head takes them in. Raises rather
        than returning short: a caller that asked for eight tips and got five
        would pick five and fail on the sixth, at the instrument.

        This is what makes a rack reusable across runs. Without it an author
        hard-codes a column, and the second run picks the same empty one.
        """
        contents = await self.of(labware)
        if not contents.is_known:
            raise ContentsUnknown(
                f"nothing has ever said what {labware.name!r} holds, so its next "
                f"tips cannot be chosen. State the layout with set-tip-state."
            )
        available = sorted(contents.tip_positions_present, key=_column_major)
        if len(available) < count:
            raise NotEnoughTips(
                f"{labware.name!r} holds {len(available)} tips; {count} were asked for"
            )
        return available[:count]

    # -- Divergence ----------------------------------------------------------

    async def divergence(
        self, labware: LabwareRef,
    ) -> ContentsDivergence | None:
        """What the driver last reported, where it disagrees with the record.

        None when the driver has said nothing, or when the two agree.
        """
        ops = await self.ops_of(labware)
        reported = latest_driver_report(ops, labware.name)
        if reported is None or reported.tip_positions_present is None:
            return None
        if not has_tip_baseline(ops, labware.name):
            return None
        ledger = sorted(tips_present(ops, labware.name))
        driver = sorted(reported.tip_positions_present)
        if ledger == driver:
            return None
        return ContentsDivergence(
            labware_id=labware.id,
            labware_name=labware.name,
            ledger_tip_positions=ledger,
            driver_tip_positions=driver,
        )
