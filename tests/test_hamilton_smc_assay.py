"""Tests for the Hamilton SMC assay example (examples/hamilton_smc).

This assay authors real inline liquid-handler methods pushed to the driver via
PyLabRobot across a Hamilton ML STAR pair (mlstar_1 capture/detection, mlstar_2
elution/neutralize). It is a representative bead-capture IL-6 immunoassay: the
whole 96-well plate is processed, every liquid-handler step is
an 8-channel transfer looped over all 12 columns, reagents are deck-resident
troughs, and up to four assay plates pool into the 384-well read plate (this
single-plate run writes the read plate's quadrant 0).

The full assay runs ONCE per module (`hamilton_run` fixture) with MethodTracker +
LabwareJourneyTracker attached (read-only observers). The pipetting operations are
read back from ``system.ops_history`` -- the ISystem-owned single source of truth
that every tracked device writes to via its operation interpreter, driver-agnostic.
Individual tests assert:

1. deck-site derivation (fast, structural);
2. every thread's method sequence is exactly right;
3. every labware's physical journey is correct start-to-end;
4. each ML STAR executes the exact inline pipetting the assay calls for --
   the ordered per-reagent transfer blocks, each spanning all 12 columns at 8
   channels and the declared volume -- never a protocol string;
5. the tracked well volumes match the recipe across the whole 96-well plate (bead
   draw-down, read-plate fill, neutralization-plate net-zero).

Expected values are derived from the assay recipe (see the workflow
docstring), not merely from whatever the run emitted, so the tests fail
loudly if a step regresses into a plausible-but-wrong shape.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass

import pytest

from examples.hamilton_smc.hamilton_smc_example import WORKFLOW_NAME, build_hamilton_smc

from orca.plugins import LabwareJourneyTracker, MethodTracker
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    DispenseDetails,
    OperationRecord,
)
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from tests.test_helpers import assert_translator_carriage_pairs, execution_outcome, named_for_template, template_of


# Deck-resident reagents (LEAVE_IN_PLACE) by template -> trough site: beads +
# detection on mlstar_1's reagent carrier, elution Buffer B + Buffer D on mlstar_2's.
_MLSTAR_1_RESIDENTS = {
    "bead_reservoir": "mlstar_1/carrier-25-0",
    "detection_reservoir": "mlstar_1/carrier-25-1",
}
_MLSTAR_2_RESIDENTS = {
    "buffer_b_reservoir": "mlstar_2/carrier-25-0",
    "buffer_d_reservoir": "mlstar_2/carrier-25-1",
}
_RESIDENT_SITES = {**_MLSTAR_1_RESIDENTS, **_MLSTAR_2_RESIDENTS}

# One tip rack per pipetting action; each rack's thread runs delid then its one
# consuming method.
_TIP_CONSUMERS = {
    "tips_sample": "target_capture",
    "tips_beads": "target_capture",
    "tips_detection": "add_detection_antibody",
    "tips_read": "transfer_to_read_plate",
    "tips_buffer_b": "add_elution_buffer_b",
    "tips_buffer_d": "neutralize_and_transfer",
    "tips_eluate": "neutralize_and_transfer",
}


@dataclass
class HamiltonRun:
    method_tracker: MethodTracker
    journey_tracker: LabwareJourneyTracker
    all_ops: list[OperationRecord]


async def _run_hamilton() -> HamiltonRun:
    build = await build_hamilton_smc()
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    method_tracker = MethodTracker()
    journey_tracker = LabwareJourneyTracker()
    runtime.register_plugin(method_tracker)
    runtime.register_plugin(journey_tracker)

    template = build.system.get_workflow_template(WORKFLOW_NAME)
    await runtime.start()
    submission = await runtime.submit(template, mode=WorkflowRunMode.PURE_SIM)
    final = await execution_outcome(runtime, submission, timeout=300.0)
    assert final.status == "completed", f"assay did not complete: {final.status}: {final.error}"

    history = build.system.ops_history.for_execution(submission.execution_id)
    all_ops = await history.all_operations()
    await runtime.shutdown()

    return HamiltonRun(method_tracker, journey_tracker, all_ops)


@pytest.fixture(scope="module")
def hamilton_run() -> HamiltonRun:
    return asyncio.run(_run_hamilton())


async def test_deck_sites_are_derived() -> None:
    """Both ML STAR deck configs derive addressable child sites; each carrier's
    handoff is excluded and every reagent's resident trough site exists."""
    build = await build_hamilton_smc()

    system_map = build.system.system_map
    for mlstar_name, residents in (("mlstar_1", _MLSTAR_1_RESIDENTS),
                                   ("mlstar_2", _MLSTAR_2_RESIDENTS)):
        # Every deck site is an equal flat node (handoff-ness is derived from
        # what the arm teaches), so all carrier sites are addressable.
        working = {site.position_id for site in system_map.sites_of(mlstar_name)}
        expected = {
            f"{mlstar_name}/carrier-7-0", f"{mlstar_name}/carrier-7-1",
            f"{mlstar_name}/carrier-15-0", f"{mlstar_name}/carrier-15-2",
            f"{mlstar_name}/carrier-15-3", f"{mlstar_name}/carrier-15-4",
            f"{mlstar_name}/carrier-25-0", f"{mlstar_name}/carrier-25-1",
        }
        assert expected <= working, f"{mlstar_name} missing deck sites: {expected - working}"
        # The arm-taught entry sites are ordinary deck sites too.
        assert f"{mlstar_name}/carrier-7-2" in working
        assert f"{mlstar_name}/carrier-15-1" in working
        for site in residents.values():
            assert site in working, f"resident anchor {site} is not a deck site"


async def test_translators_declare_single_carriage() -> None:
    """The two bridge translators come up on ``SimTranslatorDriver``, not the
    generic arm sim (see ``assert_translator_carriage_pairs``)."""
    build = await build_hamilton_smc()
    assert_translator_carriage_pairs(build.system.system_map)


@pytest.mark.slow
@pytest.mark.timeout(450)
class TestHamiltonSmcAssay:

    def _thread_methods(self, run: HamiltonRun, prefix: str) -> list[list[str]]:
        snaps = run.method_tracker.all_completed_snapshots
        return [
            methods for tid, methods in snaps.items()
            if run.method_tracker.thread_names.get(tid, "").startswith(prefix)
        ]

    def test_method_sequences_are_exact(self, hamilton_run: HamiltonRun) -> None:
        """Each thread runs exactly its expected method sequence, in order -- the
        assay step-for-step: capture -> 2 h -> spin -> wash -> detection -> 1 h ->
        wash -> 90 s shake -> final aspiration -> elution -> 10 min -> pellet ->
        neutralize + transfer."""
        run = hamilton_run

        assert self._thread_methods(run, "plate_1") == [[
            "target_capture", "incubate_2hrs", "post_capture_spin", "post_capture_wash",
            "add_detection_antibody", "incubate_1hr", "post_detection_wash",
            "post_detection_shake", "final_aspiration", "add_elution_buffer_b",
            "incubate_10min", "magnetic_pellet", "neutralize_and_transfer",
        ]]

        assert self._thread_methods(run, "sample_plate") == [["delid", "target_capture"]]

        assert self._thread_methods(run, "neut_plate") == [[
            "neutralize_and_transfer", "neutralize_shake", "centrifuge_neut",
            "transfer_to_read_plate",
        ]]

        assert self._thread_methods(run, "final_plate") == [[
            "transfer_to_read_plate", "centrifuge", "read",
        ]]

        # One tip rack per pipetting action: delid then its one consuming method.
        for tip_name, method in _TIP_CONSUMERS.items():
            assert self._thread_methods(run, tip_name) == [["delid", method]], (
                f"{tip_name} tip rack should run [delid, {method}]"
            )

        # Each deck-resident reagent is offered to exactly its one consuming method.
        assert self._thread_methods(run, "bead_reservoir") == [["target_capture"]]
        assert self._thread_methods(run, "detection_reservoir") == [["add_detection_antibody"]]
        assert self._thread_methods(run, "buffer_b_reservoir") == [["add_elution_buffer_b"]]
        assert self._thread_methods(run, "buffer_d_reservoir") == [["neutralize_and_transfer"]]

    def test_labware_journeys(self, hamilton_run: HamiltonRun) -> None:
        """Every labware follows its expected physical journey; residents stay put.

        The assay plate must reach mlstar_1 (capture/detection) before mlstar_2
        (elution/neutralize) -- an ordering guard so a step swap can't pass.
        """
        run = hamilton_run
        journeys = run.journey_tracker.all_completed_journeys

        def paths(prefix: str) -> list[list[str]]:
            return [
                j for tid, j in journeys.items()
                if run.journey_tracker.thread_names.get(tid, "").startswith(prefix)
            ]

        def devices_of(journey: list[str]) -> set[str]:
            return {loc.split("/")[0] for loc in journey}

        def assert_visits(journey: list[str], expected: list[str], label: str) -> None:
            visited = devices_of(journey)
            for dev in expected:
                assert dev in visited, f"{label} never visited {dev}: {' -> '.join(journey)}"

        counts: Counter[str] = Counter()
        for tid in run.journey_tracker.thread_names:
            counts[run.journey_tracker.thread_names[tid].rsplit("-", 1)[0]] += 1
        assert counts["plate_1"] == 1
        assert counts["sample_plate"] == 1
        assert counts["neut_plate"] == 1
        assert counts["final_plate"] == 1
        for tip_name in _TIP_CONSUMERS:
            assert counts[tip_name] == 1, f"expected one {tip_name} rack, got {counts[tip_name]}"

        # Assay plate: drives the whole run across BOTH ML STARs and both corridors.
        (plate_1,) = paths("plate_1")
        assert plate_1[0] == "stacker_3/slot"
        assert plate_1[-1] == "waste_1/slot"
        assert_visits(plate_1, [
            "mlstar_1", "mlstar_2", "biotek_1", "biotek_2", "centrifuge",
            "translator_1_start", "translator_1_end", "translator_2_start", "translator_2_end",
        ], "plate_1")
        assert any(d.startswith("shaker_") for d in devices_of(plate_1)), "plate_1 never shook"
        plate_devices = [loc.split("/")[0] for loc in plate_1]
        assert plate_devices.index("mlstar_1") < plate_devices.index("mlstar_2"), (
            f"plate_1 reached mlstar_2 before mlstar_1: {' -> '.join(plate_1)}"
        )

        (sample,) = paths("sample_plate")
        assert sample[0] == "stacker_1/slot"
        assert sample[-1] == "stacker_2/slot"
        assert_visits(sample, ["delidder", "mlstar_1"], "sample_plate")

        (neut,) = paths("neut_plate")
        assert neut[0] == "stacker_8/slot"
        assert neut[-1] == "waste_1/slot"
        assert_visits(neut, ["mlstar_2", "centrifuge"], "neut_plate")

        (final,) = paths("final_plate")
        assert final[0] == "stacker_4/slot"
        assert final[-1] == "plate_hotel/slot"
        assert_visits(final, ["mlstar_2", "centrifuge", "smc_pro"], "final_plate")

        # Every tip rack is delidded and reaches its consuming ML STAR.
        mlstar_1_tips = {"tips_sample", "tips_beads", "tips_detection"}
        for tip_name in _TIP_CONSUMERS:
            (path,) = paths(tip_name)
            assert_visits(path, ["delidder"], tip_name)
            expected_lh = "mlstar_1" if tip_name in mlstar_1_tips else "mlstar_2"
            assert_visits(path, [expected_lh], tip_name)

        # Deck residents never leave their anchored deck sites.
        for template_name, site in _RESIDENT_SITES.items():
            (path,) = paths(template_name)
            assert set(path) == {site}, f"{template_name} moved off its deck site: {path}"

    def _transfer_blocks(self, run: HamiltonRun, device_name: str) -> list[tuple[tuple[str, float, str, float], int]]:
        """Collapse a device's pipetting into ordered (src, vol) -> (dst, vol)
        transfer blocks with repeat counts. Asserts the ops come as full-8-channel
        aspirate/dispense pairs (one pair per plate column)."""
        # Only real pipetting: exclude initial-state / teaching records that ride the
        # same stream with an aspirate-flavoured operation but a non-transfer detail.
        ops = [
            op for op in run.all_ops
            if op.device_name == device_name
            and isinstance(op.details, (AspirateDetails, DispenseDetails))
        ]
        assert ops and len(ops) % 2 == 0, f"{device_name} has unpaired aspirate/dispense: {len(ops)}"
        blocks: list[list] = []
        for i in range(0, len(ops), 2):
            asp, disp = ops[i], ops[i + 1]
            assert isinstance(asp.details, AspirateDetails), f"{device_name} op {i} is not an aspirate"
            assert isinstance(disp.details, DispenseDetails), f"{device_name} op {i+1} is not a dispense"
            assert asp.details.volumes == [asp.details.volumes[0]] * 8, f"{device_name} aspirate not full 8-channel"
            assert disp.details.volumes == [disp.details.volumes[0]] * 8, f"{device_name} dispense not full 8-channel"
            # Blocks stay template-level: details.labware carries the instance
            # name (template-id prefix); the flow being asserted is per-template.
            block = (
                template_of(asp.details.labware), asp.details.volumes[0],
                template_of(disp.details.labware), disp.details.volumes[0],
            )
            if blocks and blocks[-1][0] == block:
                blocks[-1][1] += 1
            else:
                blocks.append([block, 1])
        return [(block, count) for block, count in blocks]

    def test_inline_operations(self, hamilton_run: HamiltonRun) -> None:
        """Each ML STAR executes the exact inline pipetting the assay calls for, in
        order, spanning all 12 columns (8 channels each), never a protocol string."""
        run = hamilton_run

        for device in ("mlstar_1", "mlstar_2"):
            ops = {op.operation for op in run.all_ops if op.device_name == device}
            assert DeviceOperation.ASPIRATE in ops and DeviceOperation.DISPENSE in ops, (
                f"{device} pushed no inline pipetting"
            )
            assert DeviceOperation.RUN_PROTOCOL not in ops, f"{device} driven by a protocol string"

        # mlstar_1: 75 uL sample then 100 uL beads into the assay plate, then 20 uL
        # detection antibody -- each reagent block covers all 12 columns.
        assert self._transfer_blocks(run, "mlstar_1") == [
            (("sample_plate", 75.0, "plate_1", 75.0), 12),
            (("bead_reservoir", 100.0, "plate_1", 100.0), 12),
            (("detection_reservoir", 20.0, "plate_1", 20.0), 12),
        ]

        # mlstar_2 elution/neutralize/read. Buffer B/D use reverse pipetting, so their
        # first aspirate over-draws to 15 uL (10 + 5 prime); every dispense stays 10 uL.
        assert self._transfer_blocks(run, "mlstar_2") == [
            (("buffer_b_reservoir", 15.0, "plate_1", 10.0), 1),
            (("buffer_b_reservoir", 10.0, "plate_1", 10.0), 11),
            (("buffer_d_reservoir", 15.0, "neut_plate", 10.0), 1),
            (("buffer_d_reservoir", 10.0, "neut_plate", 10.0), 11),
            (("plate_1", 10.0, "neut_plate", 10.0), 12),
            (("neut_plate", 20.0, "final_plate", 20.0), 12),
        ]

    def _net_volumes(self, run: HamiltonRun, labware: str) -> dict[str, float]:
        """Net pipetted volume per well of one labware (dispense adds, aspirate
        subtracts), folded straight from ops_history by target labware."""
        totals: dict[str, float] = {}
        for op in run.all_ops:
            d = op.details
            if isinstance(d, AspirateDetails) and named_for_template(d.labware, labware):
                for pos, vol in zip(d.positions, d.volumes):
                    totals[pos] = totals.get(pos, 0.0) - vol
            elif isinstance(d, DispenseDetails) and named_for_template(d.labware, labware):
                for pos, vol in zip(d.positions, d.volumes):
                    totals[pos] = totals.get(pos, 0.0) + vol
        return totals

    def test_net_volume_accounting_matches_recipe(self, hamilton_run: HamiltonRun) -> None:
        """Net pipetted volume per labware closes per the recipe across the WHOLE
        96-well plate. Conservation the ordered-op test does not state directly: the
        neutralization plate nets to zero, the read plate holds the final 20 uL in
        its quadrant-0 wells, the assay plate accumulates, the bead trough draws down.
        """
        run = hamilton_run

        # Assay plate: every well accumulates 100 beads + 75 sample + 20 detection +
        # 10 Buffer B - 10 eluate = 195 (protocol washes are opaque to the ledger).
        plate_1 = self._net_volumes(run, "plate_1")
        assert len(plate_1) == 96, f"assay plate should span 96 wells: {len(plate_1)}"
        assert set(plate_1.values()) == {195.0}, f"plate_1 net not 195 everywhere: {set(plate_1.values())}"

        # Sample plate: 75 uL drawn from every well.
        sample = self._net_volumes(run, "sample_plate")
        assert len(sample) == 96 and set(sample.values()) == {-75.0}, f"sample_plate {set(sample.values())}"

        # Neutralization plate conserves: 10 (Buffer D) + 10 (eluate) - 20 (to read) = 0.
        neut = self._net_volumes(run, "neut_plate")
        assert len(neut) == 96 and set(neut.values()) == {0.0}, f"neut_plate {set(neut.values())}"

        # Read plate: one 20 uL dispense into each of the 96 quadrant-0 wells (rows
        # A,C,E,G,I,K,M,O and odd columns) -- the interleaved 96->384 mapping.
        final = self._net_volumes(run, "final_plate")
        assert len(final) == 96 and set(final.values()) == {20.0}, f"final_plate {set(final.values())}"
        assert all(w[0] in "ACEGIKMO" and int(w[1:]) % 2 == 1 for w in final), (
            f"final_plate wells are not read-plate quadrant 0: {sorted(final)[:6]}"
        )

        # Bead trough: 100 uL drawn per well x 96 wells = 9600 uL out of the one pool.
        beads = self._net_volumes(run, "bead_reservoir")
        assert beads == {"A1": -9600.0}, f"bead_reservoir draw-down {beads}"
