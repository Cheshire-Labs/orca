"""Tests for SDK-based PLR example: run the Python SDK example in sim.

Mirrors the SMC assay test pattern with a ThreadRecorder and PLR command
verification via RecordingLiquidHandlerDriver.

The default-driver workflow runs ONCE per module via the `plr_run` fixture,
with a ThreadRecorder attached. Individual tests assert against what it
recorded. The recorder only reads events, so it does not alter execution.

test_plr_commands_reach_driver runs the liquid handler's commands on a recording
driver and runs its own workflow.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass

import pytest

from tests.test_helpers import execution_outcome

from tests.example_builds import build_plr
from tests.thread_recorder import ThreadRecorder
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from tests.memory_activity import close_memory_activity, memory_runtime
from tests.real_device_path import on_its_own_device_path, the_real_device_path
from tests.recording_handler import RecordingHandler


@dataclass
class PlrRunResult:
    recorder: ThreadRecorder


async def _run_plr_with_a_recorder() -> PlrRunResult:
    plr = await build_plr()
    runtime = await memory_runtime(plr.system, event_bus=plr.event_bus)
    recorder = ThreadRecorder()
    runtime.register_plugin(recorder)

    await runtime.start()
    submission = await runtime.submit(plr.workflow, mode=WorkflowRunMode.CHECK_RUN)
    await execution_outcome(runtime, submission, timeout=300.0)
    await runtime.shutdown()
    await close_memory_activity()

    return PlrRunResult(recorder=recorder)


@pytest.fixture(scope="module")
def plr_run() -> PlrRunResult:
    return asyncio.run(on_its_own_device_path(_run_plr_with_a_recorder))


@pytest.mark.slow
@pytest.mark.timeout(400)
class TestPlrExecution:
    """Run the PLR example in sim mode with full instrumentation."""

    def test_plr_example_runs_in_sim(self, plr_run: PlrRunResult) -> None:
        """PLR example executes in sim without errors.

        If the workflow run failed (timeout or exception), the fixture itself
        would have errored before reaching this test.
        """
        assert plr_run.recorder.completed_methods, (
            "Workflow completed but no threads recorded any methods"
        )

    def test_plr_method_tracking(self, plr_run: PlrRunResult) -> None:
        """All expected methods complete in correct per-thread order."""
        recorder = plr_run.recorder
        snapshots = recorder.completed_methods

        # -- All expected methods ran --
        expected_methods = {
            "cherry_pick_step", "dilute_step", "shake_step",
            "read_step", "evaluate_qc_step", "seal_step",
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
                if recorder.thread_names.get(tid, "").startswith(template_prefix)
            ]

        # dest_plate: cherry_pick -> dilute -> shake -> read -> QC -> branch(pass) -> seal
        dest_runs = find_thread_methods("dest_plate")
        assert len(dest_runs) == 1
        dest_methods = dest_runs[0]
        assert dest_methods[0] == "cherry_pick_step"
        assert dest_methods[1] == "dilute_step"
        assert dest_methods[2] == "shake_step"
        assert dest_methods[3] == "read_step"
        assert dest_methods[4] == "evaluate_qc_step"
        # QC passes (avg absorbance 0.675 < 1.0), so branch goes to seal
        assert dest_methods[5] == "seal_step"

        # sample_plate: participates in cherry_pick via join
        sample_runs = find_thread_methods("sample_plate")
        assert len(sample_runs) == 1
        assert sample_runs[0] == ["cherry_pick_step"]

        # tips: participates in cherry_pick and/or dilute via join
        tips_runs = find_thread_methods("tips")
        assert len(tips_runs) >= 1
        for run in tips_runs:
            for method in run:
                assert method in {"cherry_pick_step", "dilute_step"}, (
                    f"Tips thread ran unexpected method: {method}"
                )

        # reagent_trough: participates in dilute via join
        trough_runs = find_thread_methods("reagent_trough")
        assert len(trough_runs) == 1
        assert trough_runs[0] == ["dilute_step"]

    def test_plr_labware_journeys(self, plr_run: PlrRunResult) -> None:
        """Labware threads visit all expected locations."""
        recorder = plr_run.recorder
        journeys = recorder.completed_journeys

        def find_journeys(name_prefix: str) -> list[list[str]]:
            return [
                j for tid, j in journeys.items()
                if recorder.thread_names.get(tid, "").startswith(name_prefix)
            ]

        def device_of(location: str) -> str:
            """A journey records site nodes ('stacker/slot'); this is the device."""
            return location.split("/")[0]

        def assert_visits(journey: list[str], expected: list[str], label: str) -> None:
            visited = {device_of(loc) for loc in journey}
            for device in expected:
                assert device in visited, (
                    f"{label} never visited {device}. Journey: {' -> '.join(journey)}"
                )

        # -- Thread instance counts --
        template_counts: Counter[str] = Counter()
        for thread_id in recorder.thread_names:
            name = recorder.thread_names[thread_id]
            parts = name.rsplit("-", 1)
            template_counts[parts[0]] += 1
        assert template_counts["dest_plate"] == 1
        assert template_counts["sample_plate"] == 1
        assert template_counts["reagent_trough"] == 1
        assert template_counts["tips"] >= 1

        # -- dest_plate: full journey through all processing stations --
        dest_journeys = find_journeys("dest_plate")
        assert len(dest_journeys) == 1
        j = dest_journeys[0]
        assert device_of(j[0]) == "stacker", f"dest_plate should start at stacker, got {j[0]}"
        assert device_of(j[-1]) == "waste", f"dest_plate should end at waste, got {j[-1]}"
        assert_visits(j, ["liquid_handler", "reader", "sealer"], "dest_plate")
        # Must visit a shaker (pool assignment is nondeterministic)
        shaker_visits = [s for s in j if device_of(s).startswith("shaker_")]
        assert len(shaker_visits) >= 1, (
            f"dest_plate should visit a shaker, got none. Journey: {' -> '.join(j)}"
        )

        # -- sample_plate: stacker -> liquid_handler -> waste --
        sample_journeys = find_journeys("sample_plate")
        assert len(sample_journeys) == 1
        j = sample_journeys[0]
        assert device_of(j[0]) == "stacker"
        assert device_of(j[-1]) == "waste"
        assert_visits(j, ["liquid_handler"], "sample_plate")

        # -- tips: stacker -> liquid_handler -> waste --
        tips_journeys = find_journeys("tips")
        assert len(tips_journeys) >= 1
        for i, j in enumerate(tips_journeys):
            assert device_of(j[0]) == "stacker", f"tips[{i}] should start at stacker, got {j[0]}"
            assert device_of(j[-1]) == "waste", f"tips[{i}] should end at waste, got {j[-1]}"
            assert_visits(j, ["liquid_handler"], f"tips[{i}]")

        # The trough is a deck-resident reagent (REUSE_EXISTING + LEAVE_IN_PLACE
        # on carrier-25-0): it never routes, so its journey is that one deck site.
        trough_journeys = find_journeys("reagent_trough")
        assert len(trough_journeys) == 1
        j = trough_journeys[0]
        assert j == ["liquid_handler/carrier-25-0"], (
            f"resident trough should stay on its trough carrier, got: {' -> '.join(j)}"
        )


class TestPlrCommandRecording:
    """Verify PLR commands reach the driver. Uses a custom driver, so it
    requires its own workflow run separate from the shared fixture."""

    @pytest.mark.slow
    @pytest.mark.asyncio
    @pytest.mark.timeout(360)
    async def test_plr_commands_reach_driver(self) -> None:
        """PLR commands (aspirate, dispense, tip ops) reach the driver correctly."""
        recorder = RecordingHandler()
        the_real_device_path().simulate_with("liquid_handler", recorder)
        plr = await build_plr()

        runtime = await memory_runtime(plr.system, event_bus=plr.event_bus)
        await runtime.start()
        # `runtime.start()` does not walk LiquidHandler decks; the lazy
        # seam fires on first execution entry. Seed `current_run_mode`
        # explicitly and run the seam so the recorder's deck is configured
        # before the manual `initialize()` below (`initialize()` raises
        # without a prior `configure_deck()`). CHECK_RUN keeps
        # the bring-up walk off; this test's explicit `recorder.initialize()`
        # still controls the timing of the first driver init.
        current_run_mode.set(WorkflowRunMode.CHECK_RUN)
        await runtime.system.ensure_runtime_initialized()
        await recorder.initialize()
        submission = await runtime.submit(plr.workflow, mode=WorkflowRunMode.CHECK_RUN)
        await execution_outcome(runtime, submission, timeout=300.0)
        await runtime.shutdown()

        # Verify we got PLR calls at all
        assert len(recorder.calls) > 0, "No PLR calls recorded"

        # Extract method names
        methods_called = [c.method for c in recorder.calls]

        # Cherry pick action: pick_up_tips -> (aspirate, dispense) * N -> drop_tips
        assert "pick_up_tips" in methods_called, "No pick_up_tips call recorded"
        assert "aspirate" in methods_called, "No aspirate call recorded"
        assert "dispense" in methods_called, "No dispense call recorded"
        assert "drop_tips" in methods_called, "No drop_tips call recorded"

        # Verify cherry pick sequence starts with tip pickup
        first_pick = methods_called.index("pick_up_tips")
        first_asp = methods_called.index("aspirate")
        first_disp = methods_called.index("dispense")
        assert first_pick < first_asp < first_disp, (
            f"Expected pick_up_tips before aspirate before dispense. "
            f"Order: pick_up_tips@{first_pick}, aspirate@{first_asp}, dispense@{first_disp}"
        )

        # Serial dilution uses multiple tip pickup/drop cycles
        tip_pickups = [c for c in recorder.calls if c.method == "pick_up_tips"]
        tip_drops = [c for c in recorder.calls if c.method == "drop_tips"]
        # Cherry pick: 1 pickup + Serial dilute: 1 (diluent fill) + 3 (transfer steps) = 5 total
        assert len(tip_pickups) >= 5, (
            f"Expected at least 5 tip pickups (1 cherry pick + 4 dilution), got {len(tip_pickups)}"
        )
        assert len(tip_drops) >= 5, (
            f"Expected at least 5 tip drops, got {len(tip_drops)}"
        )

        # Verify aspirate targets include wells from the worklist.
        # Multi-rack shape: AspirateRequest.aspirations -> [AspirateTarget{labware, positions, volumes}]
        aspirate_calls = [c for c in recorder.calls if c.method == "aspirate"]
        all_aspirated_wells: list[str] = []
        for call in aspirate_calls:
            for target in call.args["aspirations"]:
                all_aspirated_wells.extend(target["positions"])
        # Cherry pick worklist has A1-A8 source wells; verify at least one is present
        assert "A1" in all_aspirated_wells, "Expected A1 in aspirated wells (cherry pick or dilution)"

        # Verify dispense targets.
        # Multi-rack shape: DispenseRequest.dispenses -> [DispenseTarget{labware, positions, volumes}]
        dispense_calls = [c for c in recorder.calls if c.method == "dispense"]
        all_dispensed_wells: list[str] = []
        for call in dispense_calls:
            for target in call.args["dispenses"]:
                all_dispensed_wells.extend(target["positions"])
        # Serial dilution dispenses to B2, B3, B4
        assert "B2" in all_dispensed_wells, "Expected B2 in dispensed wells (serial dilution)"
        assert "B3" in all_dispensed_wells, "Expected B3 in dispensed wells (serial dilution)"
        assert "B4" in all_dispensed_wells, "Expected B4 in dispensed wells (serial dilution)"

        # Verify tip spots used.
        # Multi-rack shape: PickUpTipsRequest.picks -> [TipPick{tip_rack, positions}]
        tip_spots_used: list[str] = []
        for call in tip_pickups:
            for pick in call.args["picks"]:
                tip_spots_used.extend(pick["positions"])
        # Cherry pick uses A1, serial dilute uses C1, D1, E1, F1
        assert "A1" in tip_spots_used, "Expected tip A1 (cherry pick)"
        assert "C1" in tip_spots_used, "Expected tip C1 (dilution diluent fill)"
        assert "D1" in tip_spots_used, "Expected tip D1 (dilution transfer 1)"
        assert "E1" in tip_spots_used, "Expected tip E1 (dilution transfer 2)"
        assert "F1" in tip_spots_used, "Expected tip F1 (dilution transfer 3)"

        # Verify rich params (flow_rates, offsets_z) reach the driver
        cherry_pick_aspirates = [
            c for c in aspirate_calls if c.args.get("flow_rates") == [10.0]
        ]
        assert len(cherry_pick_aspirates) > 0, (
            "Cherry pick aspirates should have flow_rates=[10.0]"
        )
        assert cherry_pick_aspirates[0].args["offsets_z"] == [1.0], (
            "Cherry pick aspirates should have offsets_z=[1.0]"
        )

        cherry_pick_dispenses = [
            c for c in dispense_calls if c.args.get("flow_rates") == [15.0]
        ]
        assert len(cherry_pick_dispenses) > 0, (
            "Cherry pick dispenses should have flow_rates=[15.0]"
        )

        dilution_transfer_aspirates = [
            c for c in aspirate_calls if c.args.get("flow_rates") == [20.0]
        ]
        assert len(dilution_transfer_aspirates) >= 3, (
            f"Expected 3 dilution transfer aspirates with flow_rates=[20.0], "
            f"got {len(dilution_transfer_aspirates)}"
        )
