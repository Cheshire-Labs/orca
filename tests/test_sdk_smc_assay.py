"""Tests for SDK-based SMC assay: run the Python SDK example in sim.

These are the SDK equivalents of the JSON-based tests in test_system_loader.py.
They verify that the SDK pathway (orca.thread, orca.workflow, orca.spawn)
produces the same execution behavior as the JSON loader pathway.

Known differences from JSON tests:
- Delid method uses a single shared name ("delid") instead of per-thread
  unique names (delid_sample, delid_tips_96, delid_tips_384)
- Shaker assignment within shaker_collection is nondeterministic

The full SMC workflow runs ONCE per module via the `smc_run` fixture, with
both MethodTracker and LabwareJourneyTracker attached. Individual tests
assert against the captured snapshots. The trackers are read-only event
observers, so attaching both does not alter execution behavior.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass

import pytest

from examples.smc_assay.smc_assay_example import build_smc
from orca.plugins import LabwareJourneyTracker, MethodTracker
from tests.test_helpers import assert_translator_carriage_pairs, execution_outcome
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.run_modes import WorkflowRunMode


@pytest.mark.asyncio
async def test_reused_tip_rack_is_held_open_by_declared_feeders() -> None:
    """tips_384 is one rack reused across actions owned by plate_1 AND neut_plate.
    Both must declare ``contributes_to=["tips_384"]`` so its slot stays open via
    the FEEDER path (held open for the declared consumers' whole multi-second
    lifetimes). Without the declaration it falls to the worker-quiescence
    fallback, which closes the slot on a momentary no-live-worker snapshot and,
    under load, strands neut_plate's late transfer into a SECOND rack. This
    structural check is load-independent, unlike the E2E symptom it guards.
    """
    smc = await build_smc()
    assert smc.workflow is not None
    assert smc.workflow.transitive_feeders_for("tips_384") == {"plate_1", "neut_plate"}, (
        "tips_384 (reused across plate_1 and neut_plate actions) must be held "
        "open by its declared feeders, not the racy worker-quiescence fallback"
    )


@pytest.mark.asyncio
async def test_translators_declare_single_carriage() -> None:
    """The two bridge translators come up on ``SimTranslatorDriver``, not the
    generic arm sim (see ``assert_translator_carriage_pairs``)."""
    smc = await build_smc()
    assert_translator_carriage_pairs(smc.system.system_map)


@dataclass
class SmcRunResult:
    method_tracker: MethodTracker
    journey_tracker: LabwareJourneyTracker


async def _run_smc_with_trackers() -> SmcRunResult:
    smc = await build_smc()
    assert smc.workflow is not None
    runtime = SystemRuntime(smc.system, event_bus=smc.event_bus)
    method_tracker = MethodTracker()
    journey_tracker = LabwareJourneyTracker()
    runtime.register_plugin(method_tracker)
    runtime.register_plugin(journey_tracker)

    await runtime.start()
    submission = await runtime.submit(smc.workflow, mode=WorkflowRunMode.PURE_SIM)
    await execution_outcome(runtime, submission, timeout=900.0)
    await runtime.shutdown()

    return SmcRunResult(method_tracker=method_tracker, journey_tracker=journey_tracker)


@pytest.fixture(scope="module")
def smc_run() -> SmcRunResult:
    return asyncio.run(_run_smc_with_trackers())


@pytest.mark.slow
@pytest.mark.timeout(1000)
class TestSdkSmcExecution:
    """Run the SDK SMC assay in sim mode."""

    def test_sdk_smc_assay_runs_in_sim(self, smc_run: SmcRunResult) -> None:
        """SDK SMC assay executes in sim without errors.

        If the workflow run failed (timeout or exception), the fixture itself
        would have errored before reaching this test.
        """
        assert smc_run.method_tracker.all_completed_snapshots, (
            "Workflow completed but no threads recorded any methods"
        )

    def test_sdk_smc_method_tracking(self, smc_run: SmcRunResult) -> None:
        """All expected methods complete in correct per-thread order."""
        tracker = smc_run.method_tracker
        snapshots = tracker.all_completed_snapshots

        # -- All expected methods ran --
        expected_methods = {
            "target_capture", "incubate_2hrs", "post_capture_spin", "post_capture_wash",
            "add_detection_antibody", "incubate_1hr", "post_detection_wash",
            "post_detection_shake", "final_aspiration", "add_elution_buffer_b",
            "incubate_10min", "magnetic_pellet", "neutralize_and_transfer",
            "neutralize_shake", "centrifuge_neut", "transfer_to_read_plate",
            "delid", "centrifuge", "read",
        }
        all_methods_run: set[str] = set()
        for method_list in snapshots.values():
            all_methods_run.update(method_list)
        missing = expected_methods - all_methods_run
        assert not missing, f"Methods never completed: {missing}"

        # -- Per-thread method order --
        def find_thread_methods(template_prefix: str) -> list[list[str]]:
            return [
                methods for tid, methods in snapshots.items()
                if tracker.thread_names.get(tid, "").startswith(template_prefix)
            ]

        plate_1_runs = find_thread_methods("plate_1")
        assert len(plate_1_runs) == 1
        assert plate_1_runs[0] == [
            "target_capture", "incubate_2hrs", "post_capture_spin", "post_capture_wash",
            "add_detection_antibody", "incubate_1hr", "post_detection_wash",
            "post_detection_shake", "final_aspiration", "add_elution_buffer_b",
            "incubate_10min", "magnetic_pellet", "neutralize_and_transfer",
        ]

        sample_runs = find_thread_methods("sample_plate")
        assert len(sample_runs) == 1
        assert sample_runs[0] == ["delid", "target_capture"]

        neut_runs = find_thread_methods("neut_plate")
        assert len(neut_runs) == 1
        assert neut_runs[0] == [
            "neutralize_and_transfer", "neutralize_shake", "centrifuge_neut",
            "transfer_to_read_plate",
        ]

        final_runs = find_thread_methods("final_plate")
        assert len(final_runs) == 1
        assert final_runs[0] == ["transfer_to_read_plate", "centrifuge", "read"]

        tips_96_runs = find_thread_methods("tips_96")
        assert len(tips_96_runs) == 2
        for run in tips_96_runs:
            assert run[0] == "delid"
            assert len(run) == 2
        shared_methods_96 = {run[1] for run in tips_96_runs}
        assert shared_methods_96 == {"target_capture", "add_detection_antibody"}

        tips_384_runs = find_thread_methods("tips_384")
        assert len(tips_384_runs) == 1, (
            f"Expected 1 tips_384 thread (park/wake reuse), got {len(tips_384_runs)}"
        )
        assert tips_384_runs[0] == [
            "delid", "add_elution_buffer_b", "neutralize_and_transfer",
            "transfer_to_read_plate",
        ]

    def test_sdk_smc_labware_journeys(self, smc_run: SmcRunResult) -> None:
        """Labware threads visit all expected devices, waypoints, and locations."""
        journey_tracker = smc_run.journey_tracker
        journeys = journey_tracker.all_completed_journeys

        def find_journeys(name_prefix: str) -> list[list[str]]:
            return [
                j for tid, j in journeys.items()
                if journey_tracker.thread_names.get(tid, "").startswith(name_prefix)
            ]

        def device_of(location: str) -> str:
            """A journey records site nodes ('stacker_3/slot'); this is the device."""
            return location.split("/")[0]

        def assert_visits(journey: list[str], expected: list[str], label: str) -> None:
            visited = {device_of(loc) for loc in journey}
            for device in expected:
                assert device in visited, (
                    f"{label} never visited {device}. Journey: {' -> '.join(journey)}"
                )

        # -- Thread instance counts --
        template_counts: Counter[str] = Counter()
        for thread_id in journey_tracker.thread_names:
            name = journey_tracker.thread_names[thread_id]
            parts = name.rsplit("-", 1)
            template_counts[parts[0]] += 1
        assert template_counts["plate_1"] == 1
        assert template_counts["sample_plate"] == 1
        assert template_counts["neut_plate"] == 1
        assert template_counts["final_plate"] == 1
        assert template_counts["tips_96"] == 2
        assert template_counts["tips_384"] == 1

        # -- plate_1: full assay journey through both corridors --
        plate_1_journeys = find_journeys("plate_1")
        assert len(plate_1_journeys) == 1
        j = plate_1_journeys[0]
        assert device_of(j[0]) == "stacker_3"
        assert device_of(j[-1]) == "waste_1"
        assert_visits(j, [
            "bravo_96", "biotek_1", "biotek_2", "bravo_384", "centrifuge",
            "translator_1_start", "translator_1_end",
            "translator_2_start", "translator_2_end",
        ], "plate_1")
        shaker_visits = [s for s in j if device_of(s).startswith("shaker_")]
        assert len(shaker_visits) >= 4, (
            f"plate_1 should visit shakers at least 4 times (2hr, 1hr, 90s, 10min), "
            f"got {len(shaker_visits)}: {shaker_visits}"
        )

        # -- sample_plate: delidder -> bravo_96 --
        sample_journeys = find_journeys("sample_plate")
        assert len(sample_journeys) == 1
        j = sample_journeys[0]
        assert device_of(j[0]) == "stacker_1"
        assert device_of(j[-1]) == "stacker_2"
        assert_visits(j, ["delidder", "bravo_96"], "sample_plate")

        # -- neut_plate: bravo_384 (neutralize) -> shaker -> centrifuge -> bravo_384 --
        neut_journeys = find_journeys("neut_plate")
        assert len(neut_journeys) == 1
        j = neut_journeys[0]
        assert device_of(j[0]) == "stacker_8"
        assert device_of(j[-1]) == "waste_1"
        assert_visits(j, ["bravo_384", "centrifuge"], "neut_plate")

        # -- final_plate: bravo_384 -> centrifuge -> smc_pro -> hotel shelf 1 --
        # Single-group run: shelf 1 free + first declared = deterministic landing.
        final_journeys = find_journeys("final_plate")
        assert len(final_journeys) == 1
        j = final_journeys[0]
        assert device_of(j[0]) == "stacker_4"
        assert device_of(j[-1]) == "hotel_pad_1"
        assert_visits(j, ["bravo_384", "centrifuge", "smc_pro"], "final_plate")

        # -- tips_96: 2 instances, each delidder -> bravo_96 -> waste --
        tips_96_journeys = find_journeys("tips_96")
        assert len(tips_96_journeys) == 2
        for i, j in enumerate(tips_96_journeys):
            assert device_of(j[0]) == "stacker_5", f"tips_96[{i}] started at {j[0]}"
            assert device_of(j[-1]) == "waste_1", f"tips_96[{i}] ended at {j[-1]}"
            assert_visits(j, ["delidder", "bravo_96"], f"tips_96[{i}]")

        # -- tips_384: 1 instance, parks on a hotel shelf between 3 method uses at bravo_384 --
        tips_384_journeys = find_journeys("tips_384")
        assert len(tips_384_journeys) == 1, (
            f"Expected 1 tips_384 thread (park/wake reuse), got {len(tips_384_journeys)}"
        )
        j = tips_384_journeys[0]
        assert device_of(j[0]) == "stacker_6", f"tips_384 should start at stacker_6, got {j[0]}"
        assert device_of(j[-1]) == "stacker_7", f"tips_384 should end at stacker_7, got {j[-1]}"
        assert_visits(j, ["delidder", "bravo_384"], "tips_384")

        # The rack physically parks at stacker_7 between method uses (when queue
        # is empty). Timing-dependent: if methods queue up fast, some parks may
        # be skipped. Assert stacker_7 appears at least once between first and
        # last bravo_384 visit (proves physical park moves happen).
        bravo_visits = [i for i, loc in enumerate(j) if device_of(loc) == "bravo_384"]
        assert len(bravo_visits) >= 1, (
            f"tips_384 should visit bravo_384. Journey: {' -> '.join(j)}"
        )
        if len(bravo_visits) > 1:
            # tips racks declare shelves 2..12 only; hotel_pad_1 stays
            # reserved for read plates.
            park_shelves = {f"hotel_pad_{i}" for i in range(2, 13)}
            hotel_between = [
                i for i, loc in enumerate(j)
                if device_of(loc) in park_shelves
                and bravo_visits[0] < i < bravo_visits[-1]
            ]
            assert len(hotel_between) >= 1, (
                f"tips_384 should park on a declared hotel shelf (2..12) "
                f"between bravo_384 visits. Journey: {' -> '.join(j)}"
            )
            assert not any(
                device_of(loc) == "hotel_pad_1" for loc in j
            ), f"tips_384 must never land on hotel_pad_1. Journey: {' -> '.join(j)}"
