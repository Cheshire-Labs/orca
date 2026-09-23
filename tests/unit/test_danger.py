"""Unit tests for the @dangerous decorator, ConfirmationRequired exception,
and the action registry. Pure-logic; no runtime setup."""

import asyncio
import dataclasses
from enum import Enum, auto

import pytest

from orca.runtime.danger import (
    ActionDescriptor,
    ActionRegistry,
    ConfirmationRequired,
    DangerLevel,
    ParamSpec,
    clear_audit_trail,
    dangerous,
    describe_action,
    list_actions,
    list_audit_entries,
)


@pytest.fixture(autouse=True)
def isolate_module_registry() -> None:
    """The danger module uses a single process-wide _REGISTRY. Do not clear
    it -- action registrations belong to the module import graph. Tests
    below register into a private ActionRegistry to stay isolated."""


def test_confirmation_required_on_sync_call_without_confirm() -> None:
    # Use a private registry scenario: register with a unique name to avoid
    # collisions with other tests that also register actions.
    @dangerous(
        name="test.danger.sync_guard",
        level=DangerLevel.CRITICAL,
        message="sync op on {target}",
    )
    def op(target: str) -> str:
        return f"did {target}"

    with pytest.raises(ConfirmationRequired) as exc_info:
        op("plate_1")

    raised = exc_info.value
    assert raised.action_name == "test.danger.sync_guard"
    assert raised.descriptor.danger_level == DangerLevel.CRITICAL
    assert raised.call_args == {"target": "plate_1"}


def test_confirm_true_allows_call_sync() -> None:
    @dangerous(
        name="test.danger.sync_ok",
        level=DangerLevel.OPERATOR,
        message="sync {x}",
    )
    def op(x: int) -> int:
        return x * 2

    assert op(5, confirm=True) == 10


def test_requires_reason_enforced() -> None:
    @dangerous(
        name="test.danger.sync_reason",
        level=DangerLevel.PHYSICAL,
        message="move {labware_id} to {location}",
        requires_reason=True,
    )
    def op(labware_id: str, location: str, reason: str | None = None) -> None:
        return None

    # confirm=True but no reason -> ValueError
    with pytest.raises(ValueError, match="requires reason"):
        op("plate_1", "loc-3", confirm=True)

    # With reason, passes
    op("plate_1", "loc-3", confirm=True, reason="manual move after crash")


def test_async_danger_wrapper() -> None:
    @dangerous(
        name="test.danger.async_op",
        level=DangerLevel.CRITICAL,
        message="async {n}",
    )
    async def op(n: int) -> int:
        await asyncio.sleep(0)
        return n + 1

    # Without confirm: raises synchronously at the first wrapper call (before await).
    with pytest.raises(ConfirmationRequired):
        asyncio.run(op(3))

    # With confirm: runs to completion.
    result = asyncio.run(op(3, confirm=True))
    assert result == 4


def test_describe_action_roundtrip() -> None:
    @dangerous(
        name="test.danger.descriptor_rt",
        level=DangerLevel.OPERATOR,
        message="operate on {thing}",
    )
    def op(thing: str, count: int = 1) -> None:
        return None

    desc = describe_action("test.danger.descriptor_rt")
    assert desc.name == "test.danger.descriptor_rt"
    assert desc.danger_level == DangerLevel.OPERATOR
    assert desc.message == "operate on {thing}"
    assert not desc.requires_reason

    param_names = {p.name for p in desc.parameters}
    # 'self', 'confirm', 'reason' are skipped; 'thing' and 'count' remain.
    assert param_names == {"thing", "count"}
    thing_param = next(p for p in desc.parameters if p.name == "thing")
    count_param = next(p for p in desc.parameters if p.name == "count")
    assert thing_param.required is True
    assert thing_param.default is None
    assert count_param.required is False
    assert count_param.default == "1"


def test_list_actions_includes_registered() -> None:
    @dangerous(
        name="test.danger.listed",
        level=DangerLevel.OPERATOR,
        message="...",
    )
    def op() -> None:
        return None

    all_actions = list_actions()
    names = [a.name for a in all_actions]
    assert "test.danger.listed" in names


def test_name_collision_raises() -> None:
    name = "test.danger.collision"

    @dangerous(name=name, level=DangerLevel.OPERATOR, message="...")
    def first() -> None:
        return None

    with pytest.raises(ValueError, match="already registered"):
        @dangerous(name=name, level=DangerLevel.OPERATOR, message="...")
        def second() -> None:
            return None


def test_private_registry_isolation() -> None:
    """Using a fresh ActionRegistry proves the data structure works
    independent of module-level state."""
    reg = ActionRegistry()
    desc = ActionDescriptor(
        name="isolated.foo",
        danger_level=DangerLevel.SAFE,
        message="",
        requires_reason=False,
        parameters=(),
    )
    reg.register(desc)

    assert reg.describe("isolated.foo") is desc
    assert [d.name for d in reg.list_all()] == ["isolated.foo"]

    with pytest.raises(KeyError):
        reg.describe("isolated.does_not_exist")

    with pytest.raises(ValueError, match="collision"):
        reg.register(desc)


def test_param_spec_fields() -> None:
    """ParamSpec is a frozen dataclass; instances equal by value."""
    a = ParamSpec(name="x", type_name="int", required=True, default=None, description="")
    b = ParamSpec(name="x", type_name="int", required=True, default=None, description="")
    assert a == b


class _Orientation(Enum):
    LEFT = auto()
    RIGHT = auto()


class _FakeTeachpoint:
    """Stands in for cheshire_drivers.teachpoints.Teachpoint: a hand-written
    class (not a dataclass, not a pydantic BaseModel) that exposes to_dict()."""

    def __init__(self, position_id: str, x: float, y: float) -> None:
        self.position_id = position_id
        self.x = x
        self.y = y

    def to_dict(self) -> dict[str, float | str]:
        return {"position_id": self.position_id, "x": self.x, "y": self.y}


@dataclasses.dataclass(frozen=True)
class _FakePatch:
    field: str
    orientation: _Orientation


class _Opaque:
    pass


def test_confirmation_required_call_args_are_json_safe_for_domain_objects() -> None:
    """A refused call carrying a Teachpoint-shaped object must report its
    coordinates, not a bare Python repr -- the refusal payload is a real
    surface (CLI prompt, daemon 400 body), not just the audit log."""

    @dangerous(
        name="test.danger.teachpoint_update",
        level=DangerLevel.PHYSICAL,
        message="update {teachpoint}",
    )
    def update(teachpoint: _FakeTeachpoint) -> None:
        return None

    tp = _FakeTeachpoint("flex_1/B4-slot", x=181.246, y=-433.855)
    with pytest.raises(ConfirmationRequired) as exc_info:
        update(tp)

    assert exc_info.value.call_args == {
        "teachpoint": {"position_id": "flex_1/B4-slot", "x": 181.246, "y": -433.855},
    }


def test_audit_entry_call_args_are_json_safe_and_recurse_through_containers() -> None:
    """The audit ring buffer entry for a confirmed call must carry structured
    data: a dataclass field nested inside a list, with an Enum value inside
    it, must all come out JSON-safe."""
    clear_audit_trail()

    @dangerous(
        name="test.danger.teachpoint_patch",
        level=DangerLevel.OPERATOR,
        message="patch {teachpoint} with {patches}",
    )
    def patch(teachpoint: _FakeTeachpoint, patches: list[_FakePatch]) -> None:
        return None

    tp = _FakeTeachpoint("pad_1", x=335.588, y=90.74)
    patch(tp, [_FakePatch(field="speed", orientation=_Orientation.LEFT)], confirm=True)

    entries = list_audit_entries()
    entry = next(e for e in entries if e.action_name == "test.danger.teachpoint_patch")
    assert entry.call_args == {
        "teachpoint": {"position_id": "pad_1", "x": 335.588, "y": 90.74},
        "patches": [{"field": "speed", "orientation": "LEFT"}],
    }


def test_json_safe_fallback_reprs_opaque_objects_without_memory_address() -> None:
    """An object with no to_dict(), not a dataclass, not a BaseModel, still
    ends up JSON-safe (a string), and the repr's memory address is scrubbed
    so audit rows stay diff-friendly across runs."""

    @dangerous(
        name="test.danger.opaque_arg",
        level=DangerLevel.OPERATOR,
        message="op {thing}",
    )
    def op(thing: _Opaque) -> None:
        return None

    with pytest.raises(ConfirmationRequired) as exc_info:
        op(_Opaque())

    reported = exc_info.value.call_args["thing"]
    assert isinstance(reported, str)
    assert reported.startswith("<") and reported.endswith("_Opaque object>")
    assert "0x" not in reported
