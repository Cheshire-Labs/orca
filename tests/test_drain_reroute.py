"""Tests for the Fix 1 regression: drain must re-fire the callback with the
bare labware template name, NOT the composite slot key.

The original bug: LabwareSlot had a single ``template_name`` field that the
registry populated with the *slot key* (e.g. ``"tips:*:<submission_id>"``).
``drain_for_handoff`` passed this to the callback as the "labware name".
The callback then looked up ``get_auto_spawn_template(labware_name)`` against
the bare template registry, got None, and silently dropped the rerouted
method. Contributor threads hung forever waiting for a co-labware receiver
that never spawned.

The fix: separate ``slot_key`` (composite registry key) from
``labware_template_name`` (bare template name passed to the callback).
Also add ``require_auto_spawn_template`` so unknown names raise instead
of silently returning None.

These tests verify the fix so the regression can't sneak back in.
"""
from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware_state import LabwareSlot
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.workflow_templates import WorkflowTemplate


def _make_method(name: str) -> ExecutingMethod:
    method = MagicMock(spec=ExecutingMethod)
    method.name = name
    # ``ExecutingMethod.completed`` is an asyncio.Event set in __init__,
    # so spec=ExecutingMethod does NOT auto-provide it. drain_for_handoff
    # consults ``.completed.is_set()`` to skip already-completed methods;
    # set it to a mock returning False so test methods are routed normally.
    method.completed = MagicMock()
    method.completed.is_set.return_value = False
    return method


def _record_callback() -> tuple[
    list[tuple[str, ExecutingMethod]],
    Callable[[str, ExecutingMethod], Awaitable[None]],
]:
    calls: list[tuple[str, ExecutingMethod]] = []

    async def callback(labware_name: str, method: ExecutingMethod, run_mode: WorkflowRunMode) -> None:
        calls.append((labware_name, method))

    return calls, callback


class TestDrainCallbackReceivesBareTemplateName:
    """The regression guard: drain must call the callback with the BARE
    labware_template_name, not the composite slot_key.

    Under a real T6 submission the slot_key is a composite string like
    ``"tips:*:<submission_id>"`` and the labware_template_name is just
    ``"tips"``. The auto-spawn callback does ``get_auto_spawn_template(name)``
    against a bare-name registry; passing the composite key causes a silent
    None return and the rerouted method is lost.
    """

    @pytest.mark.asyncio
    async def test_drain_passes_labware_template_name_not_slot_key(self) -> None:
        # Composite key (like a T6 submission would produce) vs bare template name.
        slot = LabwareSlot(
            slot_key="tips:*:abc-123",
            labware_template_name="tips",
        )
        method = _make_method("dilute_step")
        slot.queue.put_nowait(method)

        calls, callback = _record_callback()
        routed = await slot.drain_for_handoff(callback, WorkflowRunMode.PURE_SIM)

        assert routed == 1
        assert len(calls) == 1
        labware_arg, method_arg = calls[0]
        assert labware_arg == "tips", (
            f"drain must pass bare labware_template_name ('tips'), not the "
            f"composite slot_key ('tips:*:abc-123'). Got: {labware_arg!r}"
        )
        assert method_arg is method

    @pytest.mark.asyncio
    async def test_drain_pending_also_uses_bare_name(self) -> None:
        # Same regression applies to items drained from pending, not just queue.
        slot = LabwareSlot(
            slot_key="reagent_trough:group-1:*",
            labware_template_name="reagent_trough",
        )
        slot.pending.append(_make_method("mix"))
        slot.pending.append(_make_method("shake"))

        calls, callback = _record_callback()
        await slot.drain_for_handoff(callback, WorkflowRunMode.PURE_SIM)

        assert [c[0] for c in calls] == ["reagent_trough", "reagent_trough"]


class TestRequireAutoSpawnTemplateRaisesOnUnknown:
    """The up-front defense: unknown labware names raise KeyError (with
    the list of known names) instead of silently returning None.

    Used by internal trusted callers; keeps silent-drop regressions from
    reintroducing the PLR hang.
    """

    def test_raises_keyerror_with_known_names_listed(self) -> None:
        template = WorkflowTemplate(name="wf")

        with pytest.raises(KeyError) as exc_info:
            template.require_auto_spawn_template("never_registered")

        assert "never_registered" in str(exc_info.value)
        # Empty registry: message lists [] as known.
        assert "Known names" in str(exc_info.value)

    def test_lists_known_names_when_registry_nonempty(self) -> None:
        # Use the registry directly to avoid needing a full ThreadTemplate
        # fixture; require_auto_spawn_template only reads the map.
        template = WorkflowTemplate(name="wf")
        fake_thread_a = MagicMock()
        fake_thread_a.name = "thread_a"
        fake_thread_a.labware_template = MagicMock(name="plate_a")
        fake_thread_b = MagicMock()
        fake_thread_b.name = "thread_b"
        fake_thread_b.labware_template = MagicMock(name="plate_b")
        template._auto_spawn_registry["plate_a"] = fake_thread_a
        template._auto_spawn_registry["plate_b"] = fake_thread_b

        with pytest.raises(KeyError) as exc_info:
            template.require_auto_spawn_template("plate_c")

        message = str(exc_info.value)
        assert "plate_c" in message
        assert "plate_a" in message
        assert "plate_b" in message

    def test_returns_template_when_registered(self) -> None:
        template = WorkflowTemplate(name="wf")
        fake_thread = MagicMock()
        fake_thread.name = "thread_x"
        template._auto_spawn_registry["plate_x"] = fake_thread

        result = template.require_auto_spawn_template("plate_x")
        assert result is fake_thread
