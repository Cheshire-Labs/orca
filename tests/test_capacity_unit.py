"""Unit tests for capacity-policy building blocks: select_strategy, slot semantics,
and the strategy classes themselves. No spawn callback or workflow harness.

Covers:
- T8: custom strategy without on_overflow raises TypeError at select time
- T11-T14: select_strategy precedence (custom > policy > default)
- T15: SequentialStashStrategy emits SLOT.{key}.OVERFLOW
- T16: LabwareSlot.has_room() returns True when policy is None
- Bonus: RejectStrategy emits SLOT.{key}.REJECTED before raising
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orca.events.event_bus import EventBus
from orca.events.event_bus_interface import IEventBus
from orca.events.execution_context import ExecutionContext
from orca.resource_models.capacity import (
    CapacityExceededError,
    CapacityPolicy,
    OverflowAction,
)
from orca.resource_models.labware_state import LabwareSlot
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.overflow_strategy import (
    IOverflowStrategy,
    IWorkflowRef,
    RejectStrategy,
    SequentialStashStrategy,
    select_strategy,
)


class _StubWorkflowRef:
    """Explicit IWorkflowRef stub. SimpleNamespace satisfies the Protocol at
    runtime but pyright doesn't recognize it structurally."""

    def __init__(self, event_bus: IEventBus, wf_id: str = "wf-1",
                 wf_name: str = "test_wf") -> None:
        self._id = wf_id
        self._name = wf_name
        self._event_bus = event_bus

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def event_bus(self) -> IEventBus:
        return self._event_bus


def _make_workflow_ref(event_bus: EventBus, wf_id: str = "wf-1",
                       wf_name: str = "test_wf") -> IWorkflowRef:
    return _StubWorkflowRef(event_bus, wf_id, wf_name)


def _make_method(name: str = "test_method") -> ExecutingMethod:
    method = MagicMock(spec=ExecutingMethod)
    method.name = name
    return method


def _capture_events(event_bus: EventBus) -> list[tuple[str, ExecutionContext]]:
    captured: list[tuple[str, ExecutionContext]] = []
    event_bus.subscribe_all(lambda name, ctx: captured.append((name, ctx)))
    return captured


class TestSelectStrategy:
    """Verifies the priority chain: custom > policy.action > default."""

    def test_returns_sequential_stash_when_policy_is_none(self) -> None:
        slot = LabwareSlot(slot_key="any_slot", labware_template_name="any_slot")
        strategy = select_strategy(slot)
        assert isinstance(strategy, SequentialStashStrategy)

    def test_returns_reject_strategy_for_reject_action(self) -> None:
        slot = LabwareSlot(
            slot_key="any_slot",
            labware_template_name="any_slot",
            policy=CapacityPolicy(max_contributions=2,
                                  overflow_action=OverflowAction.REJECT),
        )
        strategy = select_strategy(slot)
        assert isinstance(strategy, RejectStrategy)

    def test_returns_sequential_stash_for_new_action(self) -> None:
        slot = LabwareSlot(
            slot_key="any_slot",
            labware_template_name="any_slot",
            policy=CapacityPolicy(max_contributions=2,
                                  overflow_action=OverflowAction.NEW),
        )
        strategy = select_strategy(slot)
        assert isinstance(strategy, SequentialStashStrategy)

    def test_custom_takes_priority_over_reject_policy(self) -> None:
        class CustomStrategy:
            def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod,
                            workflow: object) -> None:
                pass

        custom = CustomStrategy()
        slot = LabwareSlot(
            slot_key="any_slot",
            labware_template_name="any_slot",
            policy=CapacityPolicy(max_contributions=2,
                                  overflow_action=OverflowAction.REJECT),
            overflow_strategy=custom,
        )
        assert select_strategy(slot) is custom

    def test_bad_custom_strategy_raises_typeerror_at_select_time(self) -> None:
        bad = SimpleNamespace(some_other_method=lambda: None)
        slot = LabwareSlot(slot_key="any_slot", labware_template_name="any_slot", overflow_strategy=bad)
        with pytest.raises(TypeError, match="on_overflow"):
            select_strategy(slot)


class TestLabwareSlotHasRoom:
    """has_room() is the slot-level capacity gate consulted by the spawn callback."""

    def test_returns_true_when_policy_is_none(self) -> None:
        slot = LabwareSlot(slot_key="any_slot", labware_template_name="any_slot")
        slot.contributions_to_active = 9999
        assert slot.has_room() is True

    def test_returns_true_when_under_max(self) -> None:
        slot = LabwareSlot(
            slot_key="any_slot",
            labware_template_name="any_slot",
            policy=CapacityPolicy(max_contributions=4),
        )
        slot.contributions_to_active = 3
        assert slot.has_room() is True

    def test_returns_false_when_at_max(self) -> None:
        slot = LabwareSlot(
            slot_key="any_slot",
            labware_template_name="any_slot",
            policy=CapacityPolicy(max_contributions=4),
        )
        slot.contributions_to_active = 4
        assert slot.has_room() is False


class TestSequentialStashStrategy:
    """Stashes the rejected method in slot.pending and emits an OVERFLOW event."""

    def test_appends_method_to_pending_and_emits_overflow_event(self) -> None:
        bus = EventBus()
        captured = _capture_events(bus)
        workflow = _make_workflow_ref(bus, wf_id="wf-99", wf_name="wf99")
        slot = LabwareSlot(slot_key="plate_x", labware_template_name="plate_x")
        method = _make_method("m1")

        SequentialStashStrategy().on_overflow(slot, method, workflow)

        assert list(slot.pending) == [method]
        event_names = [name for name, _ in captured]
        assert "SLOT.plate_x.OVERFLOW" in event_names

    def test_repeated_overflow_appends_in_order(self) -> None:
        bus = EventBus()
        workflow = _make_workflow_ref(bus)
        slot = LabwareSlot(slot_key="plate_x", labware_template_name="plate_x")
        m1, m2, m3 = _make_method("m1"), _make_method("m2"), _make_method("m3")

        strategy = SequentialStashStrategy()
        strategy.on_overflow(slot, m1, workflow)
        strategy.on_overflow(slot, m2, workflow)
        strategy.on_overflow(slot, m3, workflow)

        assert list(slot.pending) == [m1, m2, m3]


class TestRejectStrategy:
    """Emits SLOT.{key}.REJECTED then raises CapacityExceededError."""

    def test_emits_rejected_event_then_raises(self) -> None:
        bus = EventBus()
        captured = _capture_events(bus)
        workflow = _make_workflow_ref(bus)
        slot = LabwareSlot(
            slot_key="plate_y",
            labware_template_name="plate_y",
            policy=CapacityPolicy(max_contributions=3,
                                  overflow_action=OverflowAction.REJECT),
        )
        method = _make_method("m1")

        with pytest.raises(CapacityExceededError, match="plate_y"):
            RejectStrategy().on_overflow(slot, method, workflow)

        event_names = [name for name, _ in captured]
        assert "SLOT.plate_y.REJECTED" in event_names

    def test_does_not_append_to_pending(self) -> None:
        bus = EventBus()
        workflow = _make_workflow_ref(bus)
        slot = LabwareSlot(
            slot_key="plate_y",
            labware_template_name="plate_y",
            policy=CapacityPolicy(max_contributions=1,
                                  overflow_action=OverflowAction.REJECT),
        )
        method = _make_method("m1")

        with pytest.raises(CapacityExceededError):
            RejectStrategy().on_overflow(slot, method, workflow)

        assert len(slot.pending) == 0


class TestIOverflowStrategyProtocol:
    """Runtime-checkable protocol: a custom class with on_overflow satisfies it."""

    def test_class_with_on_overflow_satisfies_protocol(self) -> None:
        class MyStrategy:
            def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod,
                            workflow: object) -> None:
                pass

        assert isinstance(MyStrategy(), IOverflowStrategy)

    def test_class_without_on_overflow_does_not_satisfy_protocol(self) -> None:
        class NotAStrategy:
            def some_other_method(self) -> None:
                pass

        assert not isinstance(NotAStrategy(), IOverflowStrategy)
