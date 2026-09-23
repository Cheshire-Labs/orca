"""Build-time check: every labware template needs at least one thread.

The runtime assigns a labware INSTANCE to an action via
``method.assign_thread(input_template, thread.labware)``. Without a
thread tracking the template, the instance is never created, and the
action's ``ctx.labware(name)`` raises "Labware X not assigned to this
action" deep in workflow execution.

That failure surfaces too late: the operator only sees it when the
workflow runs. The check here moves the diagnosis to build time, where
the message names the orphan template and the operator can fix it
before any execution starts.

This is a system-level constraint: it lives in the shared
SdkToSystemBuilder so every example, every user-authored workflow, and
every test that calls ``orca.build_system`` gets it.
"""

import pytest

from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder


class _FakeTemplate(LabwareTemplate):
    """Concrete LabwareTemplate for tests; satisfies the abstract method."""

    async def create_instance(self) -> LabwareInstance:
        return LabwareInstance(template_name=self._name, labware_type="Plate")


class _FakeWorkflow:
    """Minimal stand-in for a WorkflowTemplate.

    `_derive_labwares` only reads `.thread_templates[*].labware_template`,
    so a fake with that shape is enough.
    """

    def __init__(self, threads: list[object]) -> None:
        self.thread_templates = threads


class _FakeThread:
    def __init__(self, labware_template: LabwareTemplate) -> None:
        self.labware_template = labware_template


class TestDeriveLabwaresOrphanCheck:

    def test_orphan_template_in_labwares_arg_raises(self) -> None:
        """A template in `labwares=` with no thread is an orphan; raise."""
        orphan = _FakeTemplate("orphan_reservoir", "Plate")
        tracked = _FakeTemplate("tracked_plate", "Plate")
        workflow = _FakeWorkflow(threads=[_FakeThread(tracked)])

        with pytest.raises(ValueError) as excinfo:
            SdkToSystemBuilder._derive_labwares(
                explicit=[orphan, tracked],
                workflows=[workflow],
            )

        message = str(excinfo.value)
        assert "orphan_reservoir" in message, (
            f"error must name the orphan template; got: {message}"
        )
        assert "tracked_plate" not in message, (
            f"tracked templates must not be flagged; got: {message}"
        )

    def test_orphan_check_lists_all_orphans(self) -> None:
        """Multiple orphans are all named in a single error."""
        orphan_a = _FakeTemplate("buffer_a", "Plate")
        orphan_b = _FakeTemplate("buffer_b", "Plate")
        tracked = _FakeTemplate("plate_1", "Plate")
        workflow = _FakeWorkflow(threads=[_FakeThread(tracked)])

        with pytest.raises(ValueError) as excinfo:
            SdkToSystemBuilder._derive_labwares(
                explicit=[orphan_a, orphan_b, tracked],
                workflows=[workflow],
            )

        message = str(excinfo.value)
        assert "buffer_a" in message
        assert "buffer_b" in message

    def test_all_templates_threaded_passes(self) -> None:
        """Every explicit template is also tracked by a thread; no error."""
        t1 = _FakeTemplate("a", "Plate")
        t2 = _FakeTemplate("b", "Plate")
        workflow = _FakeWorkflow(
            threads=[_FakeThread(t1), _FakeThread(t2)],
        )

        result = SdkToSystemBuilder._derive_labwares(
            explicit=[t1, t2],
            workflows=[workflow],
        )

        names = {lw.name for lw in result}
        assert names == {"a", "b"}

    def test_no_explicit_labwares_passes(self) -> None:
        """All templates come from threads; no explicit list; no error."""
        t1 = _FakeTemplate("a", "Plate")
        workflow = _FakeWorkflow(threads=[_FakeThread(t1)])

        result = SdkToSystemBuilder._derive_labwares(
            explicit=None,
            workflows=[workflow],
        )

        assert [lw.name for lw in result] == ["a"]

    def test_error_message_explains_the_fix(self) -> None:
        """Operator-facing message tells the operator what to do."""
        orphan = _FakeTemplate("dmso_reservoir", "Plate")
        tracked = _FakeTemplate("plate_1", "Plate")
        workflow = _FakeWorkflow(threads=[_FakeThread(tracked)])

        with pytest.raises(ValueError) as excinfo:
            SdkToSystemBuilder._derive_labwares(
                explicit=[orphan, tracked],
                workflows=[workflow],
            )

        message = str(excinfo.value).lower()
        assert "thread" in message, (
            "message must hint that adding a thread is the fix"
        )

    def test_no_threads_anywhere_skips_check(self) -> None:
        """StandaloneMethodExecutor path: no threads, labwares passed.

        When no workflow has any thread, the orphan check is skipped --
        the standalone-method execution model binds labware via a
        different mechanism (labware_start_mapping) and legitimately
        passes templates directly. The check only applies when the
        workflow execution model is in play.
        """
        t1 = _FakeTemplate("plate_a", "Plate")
        t2 = _FakeTemplate("plate_b", "Plate")
        empty_workflow = _FakeWorkflow(threads=[])

        result = SdkToSystemBuilder._derive_labwares(
            explicit=[t1, t2],
            workflows=[empty_workflow],
        )

        names = {lw.name for lw in result}
        assert names == {"plate_a", "plate_b"}, (
            "templates must pass through when no threads exist anywhere"
        )