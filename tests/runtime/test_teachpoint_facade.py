"""TeachpointFacade contract:

- Reads pass through to the per-device teachpoint store.
- Writes pass through; the store is the source of truth on uniqueness.
- Unknown device_id raises KeyError so REST/MCP can translate to 404.
"""

from typing import List, Optional

import pytest
from cheshire_drivers.teachpoints import (
    AccessConfig,
    CartesianCoordinates,
    JointCoordinates,
    Teachpoint,
)

from orca.resource_models.transporter import Transporter
from orca.runtime.facades.teachpoints import TeachpointFacade
from orca.runtime.teachpoint_service import seeded_teachpoint_service
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap


def _cartesian_tp(name: str, x: float = 100.0) -> Teachpoint:
    return Teachpoint(
        position_id=name,
        coordinates=CartesianCoordinates(
            x=x, y=200.0, z=300.0, yaw=0.0, pitch=90.0, roll=180.0,
        ),
        orientation="right",
    )


def _joint_tp(name: str) -> Teachpoint:
    return Teachpoint(
        position_id=name,
        coordinates=JointCoordinates(
            base=10.0, shoulder=20.0, elbow=30.0, wrist=40.0, rail=5.0,
        ),
    )


class _FakeSystemWithTransporters:
    """Minimal stand-in for ISystem that supports `get_transporter`
    plus the ``transporters`` iterable used by ``list_all``.
    """

    def __init__(self, transporters: dict[str, Transporter]) -> None:
        self._by_name = transporters
        self._system_map = SystemMap(ResourceRegistry())

    def get_transporter(self, name: str) -> Transporter:
        if name not in self._by_name:
            raise KeyError(f"Resource {name} not found")
        return self._by_name[name]

    @property
    def transporters(self) -> List[Transporter]:
        return list(self._by_name.values())

    @property
    def system_map(self) -> SystemMap:
        """Empty: no transporter is wired into it, so a new teachpoint has no
        edges to add and the facade's graph update is a no-op here."""
        return self._system_map


def _build_facade(
    transporter_seeds: dict[str, List[Teachpoint]],
) -> tuple[TeachpointFacade, dict[str, Transporter]]:
    """Construct a facade over real Transporters with InMemory stores.

    Avoids constructing a full SystemRuntime/System: the facade only
    reaches into `system.get_transporter(name).teachpoint_store`, so we
    wire a fake system that returns real Transporter objects.
    """
    transporters: dict[str, Transporter] = {}
    for name, seed in transporter_seeds.items():
        transporters[name] = Transporter(
            name=name,
            teachpoint_store=seeded_teachpoint_service(seed),
        )
    fake_system = _FakeSystemWithTransporters(transporters)
    facade = TeachpointFacade(fake_system)
    return facade, transporters


class TestTeachpointFacade:

    async def test_get_returns_none_when_absent(self) -> None:
        facade, _ = _build_facade({"robot1": []})
        assert await facade.get("robot1", "missing") is None

    async def test_get_returns_stored_value(self) -> None:
        facade, _ = _build_facade({"robot1": [_cartesian_tp("foo")]})
        result = await facade.get("robot1", "foo")
        assert result is not None
        assert result.position_id == "foo"

    async def test_list_returns_all_for_device(self) -> None:
        facade, _ = _build_facade(
            {"robot1": [_cartesian_tp("a"), _joint_tp("b")]},
        )
        names = {tp.position_id for tp in await facade.list("robot1")}
        assert names == {"a", "b"}

    async def test_list_scoped_to_device(self) -> None:
        facade, _ = _build_facade(
            {
                "robot1": [_cartesian_tp("a")],
                "robot2": [_joint_tp("b")],
            },
        )
        r1_names = {tp.position_id for tp in await facade.list("robot1")}
        r2_names = {tp.position_id for tp in await facade.list("robot2")}
        assert r1_names == {"a"}
        assert r2_names == {"b"}

    async def test_list_all_walks_every_transporter(self) -> None:
        """Cross-transporter listing returns (device_id, teachpoint) tuples for
        every transporter. Empty teachpoint stores contribute zero rows.
        """
        facade, _ = _build_facade(
            {
                "robot1": [_cartesian_tp("a"), _joint_tp("b")],
                "robot2": [],
                "robot3": [_cartesian_tp("c")],
            },
        )
        result = await facade.list_all()
        # 2 + 0 + 1 = 3 rows; robot2's empty store contributes nothing.
        assert len(result) == 3
        pairs = {(dev, tp.position_id) for dev, tp in result}
        assert pairs == {
            ("robot1", "a"), ("robot1", "b"), ("robot3", "c"),
        }

    async def test_add_then_get_roundtrips(self) -> None:
        facade, _ = _build_facade({"robot1": []})
        tp = _cartesian_tp("foo", x=99.0)
        await facade.add("robot1", tp, confirm=True)
        result = await facade.get("robot1", "foo")
        assert result is not None
        assert result.position_id == "foo"
        assert isinstance(result.coordinates, CartesianCoordinates)
        assert result.coordinates.x == 99.0

    async def test_update_replaces_value(self) -> None:
        facade, _ = _build_facade({"robot1": [_cartesian_tp("foo")]})
        replacement = _cartesian_tp("foo", x=42.0)
        await facade.update("robot1", replacement, confirm=True)
        result = await facade.get("robot1", "foo")
        assert result is not None
        assert isinstance(result.coordinates, CartesianCoordinates)
        assert result.coordinates.x == 42.0

    async def test_delete_returns_true_when_removed(self) -> None:
        facade, _ = _build_facade({"robot1": [_cartesian_tp("foo")]})
        assert await facade.delete("robot1", "foo", confirm=True) is True
        assert await facade.get("robot1", "foo") is None

    async def test_delete_returns_false_when_absent(self) -> None:
        facade, _ = _build_facade({"robot1": []})
        assert await facade.delete("robot1", "ghost", confirm=True) is False


class TestUnknownDevice:
    """`get_transporter` raises KeyError for missing names; the facade
    propagates so REST/MCP can translate to 404."""

    async def test_get_raises_for_unknown_device(self) -> None:
        facade, _ = _build_facade({"robot1": []})
        with pytest.raises(KeyError, match="not found"):
            await facade.get("ghost", "foo")

    async def test_list_raises_for_unknown_device(self) -> None:
        facade, _ = _build_facade({"robot1": []})
        with pytest.raises(KeyError, match="not found"):
            await facade.list("ghost")

    async def test_add_raises_for_unknown_device(self) -> None:
        facade, _ = _build_facade({"robot1": []})
        with pytest.raises(KeyError, match="not found"):
            await facade.add("ghost", _cartesian_tp("x"), confirm=True)


class TestStoreErrorPropagation:
    """The facade does not enforce safety; it propagates whatever the
    store raises. This isolates REST/MCP from impl details."""

    async def test_add_duplicate_propagates_value_error(self) -> None:
        facade, _ = _build_facade({"robot1": [_cartesian_tp("dup")]})
        with pytest.raises(ValueError, match="already registered"):
            await facade.add("robot1", _cartesian_tp("dup"), confirm=True)

    async def test_update_unknown_propagates_keyerror(self) -> None:
        facade, _ = _build_facade({"robot1": []})
        with pytest.raises(KeyError, match="not found"):
            await facade.update(
                "robot1", _cartesian_tp("ghost"), confirm=True,
            )
