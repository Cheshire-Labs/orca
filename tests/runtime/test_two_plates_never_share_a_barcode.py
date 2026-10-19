"""A barcode belongs to one labware, for good.

A register or barcode edit naming a barcode another labware holds is refused
before anything is written. A removed labware keeps its record and its barcode,
so the barcode is not handed to a new one. Any number of labware may have no
barcode.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import BarcodeInUseError
from orca.runtime.system_runtime import SystemRuntime

from tests.runtime.test_register_adopts_the_expected_labware import (
    _await_parked_at_manual_place,
    _manual_place_runtime,
)
from tests.test_system_runtime import _build_simple_system


@pytest_asyncio.fixture
async def runtime() -> AsyncIterator[SystemRuntime]:
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, labware_store=InMemoryLabwareStore())
    await rt.start()
    try:
        yield rt
    finally:
        await rt.shutdown(confirm=True)


async def test_a_register_with_a_barcode_in_use_is_refused_and_writes_nothing(
    runtime: SystemRuntime,
) -> None:
    first = await runtime.labware.register(
        "plate_96", barcode="BC-1", location="pad1", confirm=True,
    )
    before = {lw.id for lw in await runtime.labware.list_all()}

    with pytest.raises(BarcodeInUseError) as refused:
        await runtime.labware.register(
            "plate_96", barcode="BC-1", location="pad2", confirm=True,
        )

    assert refused.value.existing_labware_id == first.id
    assert {lw.id for lw in await runtime.labware.list_all()} == before
    # pad2 was not left holding a ghost: the corrected call goes through.
    retry = await runtime.labware.register(
        "plate_96", barcode="BC-2", location="pad2", confirm=True,
    )
    assert retry.current_location == "pad2"


async def test_any_number_of_labware_may_have_no_barcode(runtime: SystemRuntime) -> None:
    await runtime.labware.register("plate_96", location="pad1", confirm=True)
    await runtime.labware.register("plate_96", location="pad2", confirm=True)


async def test_a_removed_labware_keeps_its_barcode(runtime: SystemRuntime) -> None:
    first = await runtime.labware.register(
        "plate_96", barcode="BC-1", location="pad1", confirm=True,
    )
    await runtime.labware.discharge_labware(first.id, confirm=True)

    with pytest.raises(BarcodeInUseError) as refused:
        await runtime.labware.register(
            "plate_96", barcode="BC-1", location="pad1", confirm=True,
        )
    assert refused.value.existing_labware_id == first.id


async def test_an_edit_to_a_barcode_in_use_is_refused(runtime: SystemRuntime) -> None:
    await runtime.labware.register("plate_96", barcode="BC-1", location="pad1", confirm=True)
    second = await runtime.labware.register(
        "plate_96", barcode="BC-2", location="pad2", confirm=True,
    )

    with pytest.raises(BarcodeInUseError):
        await runtime.labware.edit_barcode(second.id, "BC-1", confirm=True)

    assert (await runtime.labware.get_by_id(second.id)).barcode == "BC-2"


async def test_editing_a_labware_to_its_own_barcode_is_allowed(runtime: SystemRuntime) -> None:
    snap = await runtime.labware.register(
        "plate_96", barcode="BC-1", location="pad1", confirm=True,
    )

    await runtime.labware.edit_barcode(snap.id, "BC-1", confirm=True)


async def test_a_register_at_an_awaited_slot_with_a_barcode_in_use_is_refused() -> None:
    rt, _store = await _manual_place_runtime("wf_barcode_adopt")
    try:
        submission = await rt.submit_workflow("wf_barcode_adopt", mode=WorkflowRunMode.LIVE)
        awaited_id = await _await_parked_at_manual_place(rt, submission.id)
        await rt.labware.register("plate_wf_barcode_adopt", barcode="BC-1", confirm=True)

        with pytest.raises(BarcodeInUseError):
            await rt.labware.register(
                "plate_wf_barcode_adopt", barcode="BC-1", location="pad1", confirm=True,
            )

        # The waiting thread still waits, and the corrected call adopts its labware.
        adopted = await rt.labware.register(
            "plate_wf_barcode_adopt", barcode="BC-2", location="pad1", confirm=True,
        )
        assert adopted.id == awaited_id
        assert adopted.barcode == "BC-2"
    finally:
        await rt.shutdown()


async def test_a_barcode_on_an_adopted_labware_refuses_a_later_register() -> None:
    rt, _store = await _manual_place_runtime("wf_barcode_adopt_first")
    try:
        submission = await rt.submit_workflow("wf_barcode_adopt_first", mode=WorkflowRunMode.LIVE)
        awaited_id = await _await_parked_at_manual_place(rt, submission.id)
        await rt.labware.register(
            "plate_wf_barcode_adopt_first", barcode="BC-1", location="pad1", confirm=True,
        )

        with pytest.raises(BarcodeInUseError) as refused:
            await rt.labware.register(
                "plate_wf_barcode_adopt_first", barcode="BC-1", confirm=True,
            )
        assert refused.value.existing_labware_id == awaited_id
    finally:
        await rt.shutdown()


async def test_blank_barcodes_count_as_no_barcode(runtime: SystemRuntime) -> None:
    first = await runtime.labware.register(
        "plate_96", barcode="", location="pad1", confirm=True,
    )
    second = await runtime.labware.register(
        "plate_96", barcode="", location="pad2", confirm=True,
    )

    await runtime.labware.edit_barcode(first.id, " ", confirm=True)
    await runtime.labware.edit_barcode(second.id, " ", confirm=True)

    assert (await runtime.labware.get_by_id(first.id)).barcode is None
    assert (await runtime.labware.get_by_id(second.id)).barcode is None
