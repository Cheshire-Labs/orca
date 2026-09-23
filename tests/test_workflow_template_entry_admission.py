"""Which thread-start intents `WorkflowTemplate.add_thread` admits as an entry.

Two are refused, and both refusals name something build time can actually
see. `REUSE_EXISTING` binds labware that outlives any one execution, so a
per-submission entry thread is the wrong owner for it. `immovable=True`
asserts the engine never moves the labware off `start_location`, which as an
entry deadlocks the thread against its own contract.

A manual place is not in that company. `start=("pad_1", MANUAL_PLACE)` and
`start="pad_1"` are the same thread spelled two ways -- same flags, same
`ManualPlaceSpawn`, same `AWAITING_MANUAL_PLACE` park under LIVE. Refusing
one and admitting the other refused a spelling. The hazard that refusal was
named for, one operator place binding to two threads, is handled where it
happens: each waiting thread asks the ledger about its own labware, and a
register takes the longest-waiting expectation.
"""

from collections.abc import AsyncGenerator

import pytest

from orca.runtime.runtime_interface import (
    ImmovableThreadCannotBeEntryError,
    ReuseThreadCannotBeEntryError,
)
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, MANUAL_PLACE, REUSE_EXISTING
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadFunc, ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.test_helpers import create_test_plate_template


def _trivial_func() -> ThreadFunc:
    async def fn(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        return
        yield
    return fn


class TestManualPlaceEntriesAreAdmitted:

    def test_explicit_manual_place_registers_as_an_entry(self) -> None:
        """A source-plate thread whose labware arrives by hand may say so."""
        plate = create_test_plate_template("plate_x")
        thread = ThreadTemplate(
            labware_template=plate,
            start=("pad_1", MANUAL_PLACE),
            end="pad_1",
            func=_trivial_func(),
        )
        wf = WorkflowTemplate("wf")
        wf.add_thread(thread, is_start=True)
        assert thread in wf.entry_thread_templates

    def test_bare_string_registers_as_an_entry(self) -> None:
        plate = create_test_plate_template("plate_x")
        thread = ThreadTemplate(
            labware_template=plate, start="pad_1", end="pad_1",
            func=_trivial_func(),
        )
        wf = WorkflowTemplate("wf")
        wf.add_thread(thread, is_start=True)
        assert thread in wf.entry_thread_templates

    def test_explicit_manual_place_still_registers_as_a_contributor(self) -> None:
        """The auto-spawn path stays open; it is now an alternative rather
        than the only way to write the thread."""
        plate = create_test_plate_template("plate_x")
        thread = ThreadTemplate(
            labware_template=plate,
            start=("pad_1", MANUAL_PLACE),
            end="pad_1",
            func=_trivial_func(),
        )
        wf = WorkflowTemplate("wf")
        wf.add_thread(thread, is_start=False)
        assert thread in wf.thread_templates
        assert thread not in wf.entry_thread_templates

    def test_dispense_registers_as_an_entry(self) -> None:
        """Each `device.dispense()` yields a fresh plate, so no shared
        operator slot exists to contend over."""
        plate = create_test_plate_template("plate_x")
        thread = ThreadTemplate(
            labware_template=plate,
            start=("stacker_1", DISPENSE),
            end="stacker_1",
            func=_trivial_func(),
        )
        wf = WorkflowTemplate("wf")
        wf.add_thread(thread, is_start=True)
        assert thread in wf.entry_thread_templates


class TestEntriesStillRefused:
    """The two refusals that survive: each names a contradiction visible in
    the template alone, with no submission needed to make it real."""

    def test_reuse_existing_entry_is_refused(self) -> None:
        plate = create_test_plate_template("plate_x")
        thread = ThreadTemplate(
            labware_template=plate,
            start=("trough", REUSE_EXISTING),
            end=("trough", LEAVE_IN_PLACE),
            func=_trivial_func(),
        )
        wf = WorkflowTemplate("wf")
        with pytest.raises(ReuseThreadCannotBeEntryError) as exc:
            wf.add_thread(thread, is_start=True)
        assert exc.value.thread_name == thread.name

    def test_immovable_entry_is_refused(self) -> None:
        plate = create_test_plate_template("plate_x")
        thread = ThreadTemplate(
            labware_template=plate, start="pad_1", end="pad_1",
            func=_trivial_func(), immovable=True,
        )
        wf = WorkflowTemplate("wf")
        with pytest.raises(ImmovableThreadCannotBeEntryError) as exc:
            wf.add_thread(thread, is_start=True)
        assert exc.value.thread_name == thread.name
