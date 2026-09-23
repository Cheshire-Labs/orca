"""4-submission batch pooling E2E for the Opentrons Flex SMC assay.

Four assay plates, each submitted as its own ``JOIN_EXISTING`` submission,
converge on ONE 384-well read plate (``final_plate`` is SHARED_ACROSS_GROUPS +
BATCHABLE). The BATCHABLE receiver stays open across submissions and finalizes
once every ``plate_1`` worker has finished, so each contribution is routed to
its own interleaved quadrant by ``ctx.pool_index`` and the read plate
fills all 384 wells exactly once. A convergence failure (four separate read
plates) leaves each at quadrant 0 -- 96 wells at 80 uL when folded across
instances -- which the assertions below catch; a reused quadrant would
double-fill within one plate, also caught.

This is the Hamilton SMC batch E2E re-instrumented onto Flexes; the batching,
convergence, quadrant math, and reagent behavior are identical. The 6-submission
case is the two-handoff-lane validation: each Flex has a separate plate-handoff
and tip-handoff slot, so transit traffic does not serialize on one slot (a single
shared handoff deadlocks a deep batch).

Also exercises reagent auto-fill: the shared reagent troughs are declared
``replenished`` so four plates drawing beads/detection/buffers never drain them.
"""

import asyncio

import pytest

from examples.opentrons_flex_smc.opentrons_flex_smc_example import (
    WORKFLOW_NAME,
    build_opentrons_flex_smc,
)

from orca.state.records import (
    AspirateDetails,
    DispenseDetails,
    OperationRecord,
)
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from tests.test_helpers import named_for_template


_PLATE_COUNT = 4


def _all_384_quadrant_wells() -> set[str]:
    """The full 384-well set as four interleaved 96-well quadrants (0=A1, 1=A2,
    2=B1, 3=B2). The 96->384 mapping is a bijection, so the union is exactly 384
    distinct wells -- computed independently of the example's helper."""
    wells: set[str] = set()
    for quadrant in range(4):
        row_offset, col_offset = quadrant // 2, quadrant % 2
        for i in range(8):
            for col in range(1, 13):
                wells.add(f"{chr(ord('A') + 2 * i + row_offset)}{2 * col - 1 + col_offset}")
    return wells


def _quadrant_wells(quadrant: int) -> set[str]:
    """The 96 read-plate wells of one interleaved quadrant (0=A1, 1=A2, 2=B1,
    3=B2), the region a single 96-well contribution writes."""
    row_offset, col_offset = quadrant // 2, quadrant % 2
    return {
        f"{chr(ord('A') + 2 * i + row_offset)}{2 * col - 1 + col_offset}"
        for i in range(8)
        for col in range(1, 13)
    }


def _read_plate_net(all_ops: list[OperationRecord]) -> dict[str, float]:
    """Net pipetted volume per read-plate well, folded from ops_history across
    every final_plate instance (so a convergence failure shows as double-fill)."""
    totals: dict[str, float] = {}
    for op in all_ops:
        d = op.details
        if isinstance(d, DispenseDetails) and named_for_template(d.labware, "final_plate"):
            for pos, vol in zip(d.positions, d.volumes):
                totals[pos] = totals.get(pos, 0.0) + vol
        elif isinstance(d, AspirateDetails) and named_for_template(d.labware, "final_plate"):
            for pos, vol in zip(d.positions, d.volumes):
                totals[pos] = totals.get(pos, 0.0) - vol
    return totals


def _plate_group(gid: str) -> LabwareGroup:
    return LabwareGroup(
        id=gid,
        members=(LabwareGroupMember(thread_template_name="plate_1_journey"),),
    )


async def _wait_for_workflow_boot(runtime: SystemRuntime, execution_id: str) -> None:
    """Poll until _run_workflow has attached the ExecutingWorkflow; submitting a
    join before this point fails because the inject path reads it."""
    for _ in range(200):
        if runtime._executions[execution_id].executing_workflow is not None:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("ExecutingWorkflow never attached to execution")


async def _run_join_existing_batch(
    n: int, timeout: float = 900.0
) -> tuple[list[OperationRecord], int]:
    build = await build_opentrons_flex_smc()
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    template = build.system.get_workflow_template(WORKFLOW_NAME)
    await runtime.start()

    first = await runtime.submit(
        template, groups=[_plate_group("grp-1")],
        batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.PURE_SIM,
    )
    await _wait_for_workflow_boot(runtime, first.execution_id)
    for i in range(2, n + 1):
        sub = await runtime.submit(
            template, groups=[_plate_group(f"grp-{i}")],
            batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.PURE_SIM,
        )
        assert sub.execution_id == first.execution_id, (
            f"submission {i} must join the in-flight execution, not boot a new one"
        )

    final = await asyncio.wait_for(runtime.wait_for_execution(first), timeout=timeout)
    assert final.status == "completed", f"batch did not complete: {final.status}: {final.error}"

    receivers = [t for t in runtime.list_threads(first.execution_id) if t.name.startswith("final_plate")]
    history = build.system.ops_history.for_execution(first.execution_id)
    all_ops = await history.all_operations()
    await runtime.shutdown()
    return all_ops, len(receivers)


@pytest.mark.slow
@pytest.mark.timeout(900)
@pytest.mark.asyncio
async def test_four_submissions_fill_all_384_quadrants_once() -> None:
    all_ops, receiver_count = await _run_join_existing_batch(_PLATE_COUNT)

    assert receiver_count == 1, (
        f"{_PLATE_COUNT} JOIN_EXISTING submissions must converge on ONE 384 read "
        f"plate; got {receiver_count} final_plate receivers"
    )

    final = _read_plate_net(all_ops)
    expected = _all_384_quadrant_wells()
    assert set(final) == expected, (
        "four assay plates must fill all 384 read-plate wells exactly once; "
        f"missing={sorted(expected - set(final))[:6]} extra={sorted(set(final) - expected)[:6]}"
    )
    assert len(final) == 384
    assert set(final.values()) == {20.0}, (
        "every read-plate well holds exactly one 20 uL contribution; a reused "
        f"quadrant would double-fill: {sorted(set(final.values()))}"
    )


@pytest.mark.slow
@pytest.mark.timeout(1200)
@pytest.mark.asyncio
async def test_fifth_submission_spills_to_a_second_384() -> None:
    all_ops, receiver_count = await _run_join_existing_batch(5, timeout=1100.0)

    assert receiver_count == 2, (
        "a 5th JOIN_EXISTING submission past the 4-quadrant capacity must open a "
        f"SECOND 384 read plate; got {receiver_count} final_plate receivers"
    )

    final = _read_plate_net(all_ops)
    assert set(final) == _all_384_quadrant_wells()
    assert len(final) == 384

    # Plates 1-4 fill read plate 1's four quadrants; the 5th lands on quadrant 0
    # of a fresh plate, so folded per well quadrant 0 = 40 uL, quadrants 1-3 = 20.
    q0 = _quadrant_wells(0)
    assert all(final[w] == 40.0 for w in q0), (
        f"quadrant 0 gets both plates: {sorted({final[w] for w in q0})}"
    )
    assert all(final[w] == 20.0 for w in set(final) - q0), (
        f"quadrants 1-3 written once: {sorted({final[w] for w in set(final) - q0})}"
    )


@pytest.mark.slow
@pytest.mark.timeout(1400)
@pytest.mark.asyncio
async def test_six_submissions_fill_two_384s() -> None:
    """Generality past N=5: six plates fill two 384 read plates (4 + 2). Proves
    the pooling/overflow is capacity-driven, not hardcoded to five. With two
    handoff lanes per Flex, this deep batch runs without the transit-slot
    serialization a single shared handoff would deadlock on."""
    all_ops, receiver_count = await _run_join_existing_batch(6, timeout=1300.0)

    assert receiver_count == 2, (
        "six JOIN_EXISTING submissions must fill TWO 384 read plates (4 + 2); "
        f"got {receiver_count} final_plate receivers"
    )

    final = _read_plate_net(all_ops)
    assert set(final) == _all_384_quadrant_wells()
    assert len(final) == 384

    # Plates 5-6 land on the overflow plate's quadrants 0-1 (pool_index resets), so
    # folded across both plates quadrants 0-1 hold two 20 uL fills (40), 2-3 one.
    doubled = _quadrant_wells(0) | _quadrant_wells(1)
    assert all(final[w] == 40.0 for w in doubled), (
        f"quadrants 0-1 get two plates each: {sorted({final[w] for w in doubled})}"
    )
    assert all(final[w] == 20.0 for w in set(final) - doubled), (
        f"quadrants 2-3 written once: {sorted({final[w] for w in set(final) - doubled})}"
    )
