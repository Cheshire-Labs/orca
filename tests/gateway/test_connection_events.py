"""Tests for the ConnectionEventBus + late-connect collision check."""

from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from cheshire_drivers.gateway_protocol import (
    DeviceConnectInfo,
    DeviceLinkInfo,
    DeviceStatusInfo,
)
from orca.gateway.websocket.collision_check import (
    CollisionViolation,
    check_collision,
    format_violations_for_close_reason,
)
from orca.gateway.websocket.connection_events import ConnectionEventBus


def _device(
    name: str = "shaker_1",
    type: str = "shaker",
    interfaces: frozenset[str] | None = None,
) -> DeviceConnectInfo:
    return DeviceConnectInfo(
        name=name,
        type=type,
        interfaces=interfaces if interfaces is not None else frozenset({"IShaker"}),
        capabilities=frozenset(),
        provides_state=False,
        methods={},
    )


@pytest.mark.asyncio
class TestConnectionEventBus:
    async def test_emit_connected_calls_every_listener(self) -> None:
        bus = ConnectionEventBus()
        called: List[Tuple[str, str]] = []

        async def listener_a(device: DeviceConnectInfo, client_id: str) -> None:
            called.append((f"a:{device.name}", client_id))

        async def listener_b(device: DeviceConnectInfo, client_id: str) -> None:
            called.append((f"b:{device.name}", client_id))

        bus.subscribe_connected(listener_a)
        bus.subscribe_connected(listener_b)

        await bus.emit_connected(_device("shaker_1"), "client_x")

        assert called == [("a:shaker_1", "client_x"), ("b:shaker_1", "client_x")]

    async def test_emit_disconnected_calls_every_listener(self) -> None:
        bus = ConnectionEventBus()
        called: List[str] = []

        async def listener(device_id: str) -> None:
            called.append(device_id)

        bus.subscribe_disconnected(listener)
        await bus.emit_disconnected("shaker_1")

        assert called == ["shaker_1"]

    async def test_emit_reported_calls_every_listener(self) -> None:
        """The runtime learns a device is not brought up from this event."""
        bus = ConnectionEventBus()
        called: List[Tuple[str, bool]] = []

        async def listener(device_name: str, reported: DeviceStatusInfo) -> None:
            called.append((device_name, reported.observed_link.is_initialized))

        bus.subscribe_reported(listener)
        await bus.emit_reported(
            "shaker_1",
            DeviceStatusInfo(
                status="ready",
                links={"LIVE": DeviceLinkInfo(
                    is_connected=True, is_initialized=False,
                )},
            ),
        )

        assert called == [("shaker_1", False)]

    async def test_listener_exception_does_not_block_others(self) -> None:
        """One listener raising must not skip subsequent listeners."""
        bus = ConnectionEventBus()
        called: List[str] = []

        async def boom(device_id: str) -> None:
            raise RuntimeError("listener failure")

        async def good(device_id: str) -> None:
            called.append(device_id)

        bus.subscribe_disconnected(boom)
        bus.subscribe_disconnected(good)

        await bus.emit_disconnected("shaker_1")

        assert called == ["shaker_1"]

    async def test_unsubscribe_removes_listener(self) -> None:
        bus = ConnectionEventBus()
        called: List[str] = []

        async def listener(device_id: str) -> None:
            called.append(device_id)

        bus.subscribe_disconnected(listener)
        bus.unsubscribe_disconnected(listener)
        await bus.emit_disconnected("shaker_1")

        assert called == []

    async def test_clear_drops_all(self) -> None:
        """Every list, or a listener leaks between test cases that rely on this."""
        bus = ConnectionEventBus()
        called: List[str] = []

        async def listener(device_id: str) -> None:
            called.append(device_id)

        async def reported_listener(
            device_name: str, reported: DeviceStatusInfo,
        ) -> None:
            called.append(device_name)

        bus.subscribe_disconnected(listener)
        bus.subscribe_reported(reported_listener)
        bus.clear()
        await bus.emit_disconnected("shaker_1")
        await bus.emit_reported(
            "shaker_1",
            DeviceStatusInfo(
                status="ready",
                links={"LIVE": DeviceLinkInfo(
                    is_connected=True, is_initialized=True,
                )},
            ),
        )

        assert called == []


def _registry_with_topology(
    declared_kind: str = "shaker",
    declared_interfaces: frozenset[str] | None = None,
    class_defaults: bool = False,
) -> MagicMock:
    """Build a mock runtime whose registry returns a topology card.

    The registry's `.get(name)` returns an entry whose topology card
    matches the args. Connection card is None (irrelevant for collision
    checking against topology).
    """
    runtime = MagicMock()
    entry = MagicMock()
    entry.topology_card.declared_kind = declared_kind
    entry.topology_card.declared_interfaces = (
        declared_interfaces if declared_interfaces is not None
        else frozenset({"IShaker"})
    )
    # Set explicitly: a MagicMock attribute reads truthy, which would skip the
    # superset rule in every test below without saying so.
    entry.topology_card.declared_interfaces_are_class_defaults = class_defaults
    entry.connection_card = None
    runtime.device_registry.get = AsyncMock(return_value=entry)
    return runtime


@pytest.mark.asyncio
class TestCheckCollision:
    async def test_runtime_none_returns_empty_list(self) -> None:
        out = await check_collision(None, [_device()])
        assert out == []

    async def test_a_class_default_set_does_not_close_the_link(self) -> None:
        """Nothing was declared, so a narrower client has broken no contract.

        A violation closes the whole WebSocket with 1002, which takes every
        device on that box offline at once, mid-run. Doing that over a set the
        deployment author never wrote is the wrong trade.
        """
        runtime = _registry_with_topology(
            declared_interfaces=frozenset({"IShaker", "ITempSettable"}),
            class_defaults=True,
        )

        out = await check_collision(
            runtime, [_device(interfaces=frozenset({"IShaker"}))],
        )

        assert out == []

    async def test_a_class_default_that_shares_nothing_is_still_said_out_loud(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A rollback narrows the set; a wrong device shares nothing with it.

        Both are accepted, because closing the link is worse than a
        per-command refusal. Only the second is a config error, and nothing
        else would ever mention it.
        """
        runtime = _registry_with_topology(
            declared_interfaces=frozenset({"IShaker"}),
            class_defaults=True,
        )

        with caplog.at_level(
            "ERROR", logger="orca.gateway.websocket.collision_check",
        ):
            out = await check_collision(
                runtime, [_device(interfaces=frozenset({"ICentrifuge"}))],
            )

        assert out == []
        assert any(
            "share nothing" in rec.message for rec in caplog.records
        ), f"expected a mismatch error, got {[r.message for r in caplog.records]}"

    async def test_a_narrower_class_default_says_nothing(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The rollback case is ordinary. An error per connect is an error log
        nobody reads."""
        runtime = _registry_with_topology(
            declared_interfaces=frozenset({"IShaker", "ITempSettable"}),
            class_defaults=True,
        )

        with caplog.at_level(
            "ERROR", logger="orca.gateway.websocket.collision_check",
        ):
            out = await check_collision(
                runtime, [_device(interfaces=frozenset({"IShaker"}))],
            )

        assert out == []
        assert caplog.records == []

    async def test_a_real_declaration_still_holds(self) -> None:
        """The deployment author wrote this set, so a client missing part of
        it is a contract break and still fails loud."""
        runtime = _registry_with_topology(
            declared_interfaces=frozenset({"IShaker", "ITempSettable"}),
            class_defaults=False,
        )

        out = await check_collision(
            runtime, [_device(interfaces=frozenset({"IShaker"}))],
        )

        assert len(out) == 1
        assert "ITempSettable" in out[0].reason

    async def test_topology_only_q6_quarantine_passes(self) -> None:
        """Connection-only devices (no topology card) pass without comparison."""
        runtime = MagicMock()
        entry = MagicMock()
        entry.topology_card = None
        runtime.device_registry.get = AsyncMock(return_value=entry)

        out = await check_collision(runtime, [_device()])
        assert out == []

    async def test_undeclared_device_no_entry_warns(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A connecting device absent from topology is accepted but warned.

        At connect time the registry has no entry (not declared, not yet
        registered). The device is quarantined (no violation) but a warning
        fires so a typo'd / misconfigured device name is visible.
        """
        runtime = MagicMock()
        runtime.device_registry.get = AsyncMock(return_value=None)
        with caplog.at_level(
            "WARNING", logger="orca.gateway.websocket.collision_check",
        ):
            out = await check_collision(runtime, [_device(name="ghost_1")])
        assert out == []
        assert any(
            "not declared in topology" in rec.message and "ghost_1" in rec.message
            for rec in caplog.records
        ), f"expected undeclared-device warning, got {[r.message for r in caplog.records]}"

    async def test_undeclared_device_topology_card_none_warns(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A connected device with a registry entry but no topology card warns."""
        runtime = MagicMock()
        entry = MagicMock()
        entry.topology_card = None
        runtime.device_registry.get = AsyncMock(return_value=entry)
        with caplog.at_level(
            "WARNING", logger="orca.gateway.websocket.collision_check",
        ):
            out = await check_collision(runtime, [_device(name="ghost_2")])
        assert out == []
        assert any(
            "not declared in topology" in rec.message and "ghost_2" in rec.message
            for rec in caplog.records
        )

    async def test_declared_device_does_not_warn_undeclared(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A declared device passes the interface check and emits no ghost warning."""
        runtime = _registry_with_topology(
            declared_kind="shaker", declared_interfaces=frozenset({"IShaker"}),
        )
        with caplog.at_level(
            "WARNING", logger="orca.gateway.websocket.collision_check",
        ):
            out = await check_collision(
                runtime,
                [_device(type="shaker", interfaces=frozenset({"IShaker"}))],
            )
        assert out == []
        assert not any(
            "not declared in topology" in rec.message for rec in caplog.records
        )

    async def test_no_entry_returns_empty(self) -> None:
        runtime = MagicMock()
        runtime.device_registry.get = AsyncMock(return_value=None)

        out = await check_collision(runtime, [_device()])
        assert out == []

    async def test_kind_match_and_iface_superset_passes(self) -> None:
        runtime = _registry_with_topology(
            declared_kind="shaker", declared_interfaces=frozenset({"IShaker"}),
        )
        out = await check_collision(
            runtime,
            [_device(type="shaker", interfaces=frozenset({"IShaker", "IReader"}))],
        )
        assert out == []

    async def test_kind_drift_alone_does_not_violate(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Kind drift is advisory: log warning, accept if interfaces satisfy.

        The safety contract is the interface superset rule. Different kind
        labels can satisfy the same interface contract (a "shaker" and a
        "thermal_shaker" can both implement IShaker), so kind drift must
        not block a connection that would dispatch correctly.
        """
        runtime = _registry_with_topology(
            declared_kind="shaker", declared_interfaces=frozenset({"IShaker"}),
        )
        with caplog.at_level("WARNING", logger="orca.gateway.websocket.collision_check"):
            out = await check_collision(
                runtime,
                [_device(
                    name="shaker_1",
                    type="thermal_shaker",
                    interfaces=frozenset({"IShaker"}),
                )],
            )
        assert out == []
        assert any(
            "kind drift" in rec.message and "shaker_1" in rec.message
            for rec in caplog.records
        ), f"expected kind drift warning, got {[r.message for r in caplog.records]}"

    async def test_kind_drift_with_interface_break_reports_interface_break(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Kind drift logs a warning AND interface check still runs.

        Pre-fix the kind violation short-circuited (continue) so the
        interface check never ran. Post-fix kind drift only logs and the
        interface superset rule is the actual decision.
        """
        runtime = _registry_with_topology(
            declared_kind="shaker",
            declared_interfaces=frozenset({"IShaker", "ITempSettable"}),
        )
        with caplog.at_level("WARNING", logger="orca.gateway.websocket.collision_check"):
            out = await check_collision(
                runtime,
                [_device(
                    name="shaker_1",
                    type="thermal_shaker",
                    interfaces=frozenset({"IShaker"}),  # missing ITempSettable
                )],
            )
        assert len(out) == 1
        assert "interface contract break" in out[0].reason
        assert any("kind drift" in rec.message for rec in caplog.records)

    async def test_iface_subset_violates_contract(self) -> None:
        """Topology declares more than orca-client advertises => contract break."""
        runtime = _registry_with_topology(
            declared_kind="shaker",
            declared_interfaces=frozenset({"IShaker", "ITempSettable"}),
        )
        out = await check_collision(
            runtime,
            [_device(type="shaker", interfaces=frozenset({"IShaker"}))],
        )
        assert len(out) == 1
        assert "interface contract break" in out[0].reason
        assert out[0].advertised_interfaces == frozenset({"IShaker"})

    async def test_registry_lookup_exception_skipped(self) -> None:
        """A registry lookup error skips the device but doesn't kill the check."""
        runtime = MagicMock()
        runtime.device_registry.get = AsyncMock(side_effect=ValueError("boom"))

        out = await check_collision(runtime, [_device()])
        # Skipped on error => no violation reported.
        assert out == []

    async def test_iface_exact_match_passes(self) -> None:
        """Advertised set equal to declared set is a valid superset."""
        runtime = _registry_with_topology(
            declared_kind="shaker", declared_interfaces=frozenset({"IShaker"}),
        )
        out = await check_collision(
            runtime,
            [_device(type="shaker", interfaces=frozenset({"IShaker"}))],
        )
        assert out == []

    async def test_iface_extra_advertised_passes(self) -> None:
        """Advertising more than declared (proper superset) is fine."""
        runtime = _registry_with_topology(
            declared_kind="shaker", declared_interfaces=frozenset({"IShaker"}),
        )
        out = await check_collision(
            runtime,
            [_device(
                type="shaker",
                interfaces=frozenset({"IShaker", "IReader", "ITempSettable"}),
            )],
        )
        assert out == []

    async def test_empty_declared_interfaces_accepts_anything(self) -> None:
        """A topology card with no declared interfaces imposes no contract.

        The empty set is a subset of every set, so superset always holds.
        Edge case worth pinning so a future refactor doesn't accidentally
        reject empty-contract devices.
        """
        runtime = _registry_with_topology(
            declared_kind="shaker", declared_interfaces=frozenset(),
        )
        out = await check_collision(
            runtime,
            [_device(type="shaker", interfaces=frozenset())],
        )
        assert out == []

    async def test_multi_device_only_failures_reported(self) -> None:
        """Mix of pass + fail devices: only the failing one shows up."""
        good_entry = MagicMock()
        good_entry.topology_card.declared_kind = "shaker"
        good_entry.topology_card.declared_interfaces = frozenset({"IShaker"})
        good_entry.topology_card.declared_interfaces_are_class_defaults = False
        good_entry.connection_card = None

        bad_entry = MagicMock()
        bad_entry.topology_card.declared_kind = "centrifuge"
        bad_entry.topology_card.declared_interfaces = frozenset(
            {"ICentrifuge", "ISpinSettable"},
        )
        bad_entry.topology_card.declared_interfaces_are_class_defaults = False
        bad_entry.connection_card = None

        async def lookup(device_id: str) -> MagicMock:
            return {"shaker_1": good_entry, "centrifuge_1": bad_entry}[device_id]

        runtime = MagicMock()
        runtime.device_registry.get = AsyncMock(side_effect=lookup)

        out = await check_collision(
            runtime,
            [
                _device(
                    name="shaker_1",
                    type="shaker",
                    interfaces=frozenset({"IShaker"}),
                ),
                _device(
                    name="centrifuge_1",
                    type="centrifuge",
                    interfaces=frozenset({"ICentrifuge"}),  # missing ISpinSettable
                ),
            ],
        )
        assert len(out) == 1
        assert out[0].name == "centrifuge_1"
        assert "interface contract break" in out[0].reason

    async def test_multi_device_all_pass(self) -> None:
        """Every device satisfying its contract returns no violations."""
        entry_a = MagicMock()
        entry_a.topology_card.declared_kind = "shaker"
        entry_a.topology_card.declared_interfaces = frozenset({"IShaker"})
        entry_a.connection_card = None

        entry_b = MagicMock()
        entry_b.topology_card.declared_kind = "centrifuge"
        entry_b.topology_card.declared_interfaces = frozenset({"ICentrifuge"})
        entry_b.connection_card = None

        async def lookup(device_id: str) -> MagicMock:
            return {"shaker_1": entry_a, "centrifuge_1": entry_b}[device_id]

        runtime = MagicMock()
        runtime.device_registry.get = AsyncMock(side_effect=lookup)

        out = await check_collision(
            runtime,
            [
                _device(
                    name="shaker_1",
                    type="shaker",
                    interfaces=frozenset({"IShaker"}),
                ),
                _device(
                    name="centrifuge_1",
                    type="centrifuge",
                    interfaces=frozenset({"ICentrifuge", "IExtra"}),
                ),
            ],
        )
        assert out == []


class TestFormatViolations:
    def test_empty_list_returns_empty_string(self) -> None:
        assert format_violations_for_close_reason([]) == ""

    def test_single_violation_includes_id_and_reason(self) -> None:
        v = CollisionViolation(
            name="shaker_1",
            declared_kind="shaker",
            advertised_kind="shaker",
            declared_interfaces=frozenset({"IShaker", "ITempSettable"}),
            advertised_interfaces=frozenset({"IShaker"}),
            reason="interface contract break: topology declared "
                   "['IShaker', 'ITempSettable'] but the device bridge advertised "
                   "['IShaker']; missing ['ITempSettable']",
        )
        out = format_violations_for_close_reason([v])
        assert "shaker_1" in out
        assert "interface contract break" in out

    def test_multi_violation_truncates_to_first(self) -> None:
        v1 = CollisionViolation(
            name="d1",
            declared_kind="shaker",
            advertised_kind="centrifuge",
            declared_interfaces=frozenset(),
            advertised_interfaces=frozenset(),
            reason="r1",
        )
        v2 = CollisionViolation(
            name="d2",
            declared_kind="shaker",
            advertised_kind="reader",
            declared_interfaces=frozenset(),
            advertised_interfaces=frozenset(),
            reason="r2",
        )
        out = format_violations_for_close_reason([v1, v2])
        assert "2 devices" in out
        assert "d1" in out

    def test_long_reason_truncated_to_120_bytes(self) -> None:
        v = CollisionViolation(
            name="d" * 200,  # very long device id forces overflow
            declared_kind="shaker",
            advertised_kind="centrifuge",
            declared_interfaces=frozenset(),
            advertised_interfaces=frozenset(),
            reason="r",
        )
        out = format_violations_for_close_reason([v])
        assert len(out.encode("utf-8")) <= 120
