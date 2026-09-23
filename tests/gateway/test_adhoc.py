"""Unit tests for :mod:`orca.gateway.adhoc`, the generic ad-hoc device-command core.

This module holds the device-resolution + command-execution mechanism extracted
out of a hosted deployment's gateway service. Unlike the hosted wrapper, ``adhoc`` RAISES the
orca controller exceptions / ``RuntimeError`` straight through rather than mapping
them to ``OrcaApiError`` tuples, and returns a typed :class:`AdhocResult`.

Covered here:
  * :func:`resolve_effective_interfaces` four-corner matrix (topology x connection).
  * :func:`_resolve_external_control_target` resolver behavior.
  * :func:`resolve_device` raise-on-miss behavior + snapshot return.
  * :func:`execute_adhoc_command` success result, effective-interface threading,
    external-control take/release wiring, and the ``effective_mode`` it stamps.

An ad-hoc command is an operator asking a device to act, so it dispatches
against a LIVE base. The suite-wide conftest seeds ``current_run_mode``, which
would mask that entirely, so every mode assertion here runs in a fresh
``contextvars.Context`` -- the state an operator request is really in.
"""

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orca.runtime.run_modes import WorkflowRunMode, current_run_mode

from orca.gateway.adhoc import AdhocResult
from orca.gateway.controller.exceptions import (
    DeviceOfflineError,
    DeviceUnknownError,
    ModeUnresolvableError,
)
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.gateway import adhoc
from tests.gateway.mode_doubles import DeclaredDevice, unseeded_await


def _snapshot(device_id: str = "dev_1", device_type: str = "shaker") -> DeviceSnapshot:
    return DeviceSnapshot(
        type=device_type,
        name=device_id,
        interfaces=[],
        capabilities=[],
        provides_state=False,
        methods={},
        site="test",
        lab="test",
        workcell=None,
        status="ready",
        last_seen=datetime.utcnow(),
    )


@pytest.fixture
def mock_runtime_with_device() -> MagicMock:
    """Returns a runtime mock whose topology has a single device 'dev_1'."""
    runtime = MagicMock()
    topology_entry = MagicMock()
    topology_entry.name = "dev_1"
    runtime.topology.list_devices.return_value = [topology_entry]
    return runtime


class _FakeResource:
    """A topology resource that neither takes external control nor declares a mode."""


def _runtime_declaring_nothing() -> MagicMock:
    """A built runtime whose system declares no devices.

    The reachable stand-in for "no topology entry". A runtime that is absent
    entirely is a different case: the mode is unresolvable, not defaulted.
    """
    runtime = MagicMock()
    runtime.system.has_resource.return_value = False
    runtime.device_registry.get = AsyncMock(return_value=None)
    runtime.topology.list_devices.return_value = []
    return runtime


def _runtime_declaring(resource: Any) -> MagicMock:
    """A runtime whose system declares `dev_1` as `resource`."""
    runtime = MagicMock()
    runtime.system.has_resource.return_value = True
    runtime.system.get_resource.return_value = resource
    runtime.device_registry.get = AsyncMock(return_value=None)
    runtime.topology.list_devices.return_value = []
    return runtime


class _FakeExternalControllable:

    def __init__(self) -> None:
        self._held: bool = False
        self.take_calls: int = 0
        self.release_calls: int = 0

    @property
    def under_external_control(self) -> bool:
        return self._held

    def take_external_control(self) -> None:
        self._held = True
        self.take_calls += 1

    def release_external_control(self) -> None:
        self._held = False
        self.release_calls += 1


@pytest.mark.asyncio
class TestResolveEffectiveInterfaces:
    """Four-corner matrix (topology x connection):

      * both: declared INTERSECT advertised  -> blocks capability drift.
      * connection-only: advertised          -> Q6 quarantine; vendor extras still count.
      * topology-only: declared              -> dispatch unreachable in practice.
      * neither (None / no entry): None      -> controller falls back to advertised.
    """

    async def test_both_cards_returns_intersection_blocking_drift(self) -> None:
        runtime = MagicMock()
        entry = MagicMock()
        entry.topology_card.declared_interfaces = frozenset({"IShaker"})
        # A real declaration, not the driver class's default: drift is blocked.
        entry.topology_card.declared_interfaces_are_class_defaults = False
        entry.connection_card.advertised_interfaces = frozenset({"IShaker", "IReader"})
        runtime.device_registry.get = AsyncMock(return_value=entry)

        out = await adhoc.resolve_effective_interfaces(
            runtime, "dev_1", frozenset({"IShaker", "IReader"}),
        )
        assert out == frozenset({"IShaker"})

    async def test_connection_only_returns_advertised(self) -> None:
        runtime = MagicMock()
        entry = MagicMock()
        entry.topology_card = None
        entry.connection_card.advertised_interfaces = frozenset({"IShaker"})
        runtime.device_registry.get = AsyncMock(return_value=entry)

        out = await adhoc.resolve_effective_interfaces(
            runtime, "dev_1", frozenset({"IShaker"}),
        )
        assert out == frozenset({"IShaker"})

    async def test_topology_only_returns_declared(self) -> None:
        runtime = MagicMock()
        entry = MagicMock()
        entry.connection_card = None
        entry.topology_card.declared_interfaces = frozenset({"IShaker"})
        runtime.device_registry.get = AsyncMock(return_value=entry)

        out = await adhoc.resolve_effective_interfaces(
            runtime, "dev_1", frozenset(),
        )
        assert out == frozenset({"IShaker"})

    async def test_no_runtime_returns_none(self) -> None:
        out = await adhoc.resolve_effective_interfaces(
            None, "dev_1", frozenset({"IShaker"}),
        )
        assert out is None

    async def test_no_entry_returns_none(self) -> None:
        runtime = MagicMock()
        runtime.device_registry.get = AsyncMock(return_value=None)
        out = await adhoc.resolve_effective_interfaces(
            runtime, "dev_1", frozenset({"IShaker"}),
        )
        assert out is None

    async def test_registry_lookup_exception_returns_none(self) -> None:
        runtime = MagicMock()
        runtime.device_registry.get = AsyncMock(side_effect=ValueError("registry boom"))
        out = await adhoc.resolve_effective_interfaces(
            runtime, "dev_1", frozenset({"IShaker"}),
        )
        assert out is None


class TestResolveTopologyResource:
    """The external-control lookup.

    Returns whatever the system holds under that name, protocol or not; the
    caller narrows for itself. It takes a system rather than an optional one
    because it does NOT decide the dispatch mode: "no system" and "no such
    device" are the same None here and different answers for the mode, so that
    question goes through `resolve_device_mode` instead.
    """

    def test_returns_the_resource_whether_or_not_it_takes_external_control(self) -> None:
        system = MagicMock()
        system.has_resource.return_value = True
        plain = _FakeResource()
        system.get_resource.return_value = plain

        assert adhoc._resolve_topology_resource(system, "dev_1") is plain

    def test_returns_none_when_the_system_does_not_declare_it(self) -> None:
        system = MagicMock()
        system.has_resource.return_value = False

        assert adhoc._resolve_topology_resource(system, "dev_1") is None

    def test_the_resource_is_looked_up_once_per_command(self) -> None:
        """The registry is not free, so one command is one lookup."""
        system = MagicMock()
        system.has_resource.return_value = True
        system.get_resource.return_value = _FakeExternalControllable()

        adhoc._resolve_topology_resource(system, "dev_1")

        system.has_resource.assert_called_once_with("dev_1")
        system.get_resource.assert_called_once_with("dev_1")


@pytest.mark.asyncio
class TestResolveDevice:
    """``resolve_device`` raises orca controller exceptions; it does not return tuples."""

    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_returns_snapshot_when_present(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=_snapshot())

        device = await adhoc.resolve_device(None, "dev_1")

        assert device is not None
        assert device.name == "dev_1"
        assert device.type == "shaker"

    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_raises_unknown_when_not_in_topology(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=None)

        with pytest.raises(DeviceUnknownError) as exc:
            await adhoc.resolve_device(None, "ghost")

        assert "not in the running topology" in str(exc.value)

    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_raises_offline_when_in_topology_but_no_gateway(
        self,
        mock_registry: MagicMock,
        mock_runtime_with_device: MagicMock,
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=None)

        with pytest.raises(DeviceOfflineError) as exc:
            await adhoc.resolve_device(mock_runtime_with_device, "dev_1")

        assert "topology" in str(exc.value)
        assert "gateway" in str(exc.value).lower()

    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_propagates_runtime_error_from_registry(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.get_device = AsyncMock(
            side_effect=RuntimeError("missing api key")
        )

        with pytest.raises(RuntimeError) as exc:
            await adhoc.resolve_device(None, "dev_1")

        assert "missing api key" in str(exc.value)


@pytest.mark.asyncio
class TestExecuteAdhocCommand:
    """``execute_adhoc_command`` returns AdhocResult and wires external control."""

    @patch("orca.gateway.adhoc.device_controller")
    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_success_returns_adhoc_result(
        self,
        mock_registry: MagicMock,
        mock_controller: MagicMock,
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        sentinel = MagicMock(name="raw_driver_return")
        mock_controller.execute_command = AsyncMock(return_value=sentinel)

        result = await unseeded_await(adhoc.execute_adhoc_command(
            _runtime_declaring_nothing(), "dev_1", "shake", {"speed": 500}
        ))

        assert isinstance(result, AdhocResult)
        assert result.raw is sentinel
        # No card resolves -> effective_interfaces None -> falls back to
        # device.interfaces.
        assert result.interfaces == frozenset()
        mock_controller.execute_command.assert_called_once_with(
            device_id="dev_1", command="shake", params={"speed": 500},
            timeout_seconds=None,
            effective_mode=WorkflowRunMode.LIVE,
            effective_interfaces=None,
            confirm=False,
        )

    @patch("orca.gateway.adhoc.device_controller")
    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_threads_effective_interfaces_when_both_cards_present(
        self,
        mock_registry: MagicMock,
        mock_controller: MagicMock,
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(return_value=None)

        runtime = MagicMock()
        entry = MagicMock()
        entry.topology_card.declared_interfaces = frozenset({"IShaker"})
        # A real declaration, not the driver class's default: drift is blocked.
        entry.topology_card.declared_interfaces_are_class_defaults = False
        entry.connection_card.advertised_interfaces = frozenset({"IShaker", "IReader"})
        runtime.device_registry.get = AsyncMock(return_value=entry)
        runtime.topology.list_devices.return_value = []
        runtime.system.has_resource.return_value = False

        result = await unseeded_await(adhoc.execute_adhoc_command(
            runtime, "dev_1", "shake", {"speed": 500}
        ))

        mock_controller.execute_command.assert_called_once_with(
            device_id="dev_1",
            command="shake",
            params={"speed": 500},
            timeout_seconds=None,
            effective_mode=WorkflowRunMode.LIVE,
            effective_interfaces=frozenset({"IShaker"}),
            confirm=False,
        )
        assert result.interfaces == frozenset({"IShaker"})

    @patch("orca.gateway.adhoc.device_controller")
    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_effective_interfaces_none_when_no_card_resolves(
        self,
        mock_registry: MagicMock,
        mock_controller: MagicMock,
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(return_value=None)

        await unseeded_await(adhoc.execute_adhoc_command(
            _runtime_declaring_nothing(), "dev_1", "shake", {"speed": 500}
        ))

        # No registry entry -> effective_interfaces stays None so the
        # controller falls back to the connection tracker's set.
        mock_controller.execute_command.assert_called_once_with(
            device_id="dev_1", command="shake", params={"speed": 500},
            timeout_seconds=None,
            effective_mode=WorkflowRunMode.LIVE,
            effective_interfaces=None,
            confirm=False,
        )

    @patch("orca.gateway.adhoc.device_controller")
    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_take_and_release_fired_on_success(
        self,
        mock_registry: MagicMock,
        mock_controller: MagicMock,
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(return_value=None)

        resource = _FakeExternalControllable()
        runtime = MagicMock()
        runtime.system.has_resource.return_value = True
        runtime.system.get_resource.return_value = resource
        runtime.device_registry.get = AsyncMock(return_value=None)
        runtime.topology.list_devices.return_value = []

        result = await adhoc.execute_adhoc_command(
            runtime, "dev_1", "shake", {"speed": 500},
        )

        assert result is not None
        assert resource.take_calls == 1
        assert resource.release_calls == 1
        assert resource.under_external_control is False

    @patch("orca.gateway.adhoc.device_controller")
    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_release_fired_when_controller_raises(
        self,
        mock_registry: MagicMock,
        mock_controller: MagicMock,
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(
            side_effect=DeviceOfflineError("offline"),
        )

        resource = _FakeExternalControllable()
        runtime = MagicMock()
        runtime.system.has_resource.return_value = True
        runtime.system.get_resource.return_value = resource
        runtime.device_registry.get = AsyncMock(return_value=None)
        runtime.topology.list_devices.return_value = []

        with pytest.raises(DeviceOfflineError):
            await adhoc.execute_adhoc_command(runtime, "dev_1", "shake")

        assert resource.take_calls == 1
        assert resource.release_calls == 1
        assert resource.under_external_control is False

    @patch("orca.gateway.adhoc.device_controller")
    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_release_fired_on_unexpected_exception(
        self,
        mock_registry: MagicMock,
        mock_controller: MagicMock,
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(
            side_effect=ZeroDivisionError("boom"),
        )

        resource = _FakeExternalControllable()
        runtime = MagicMock()
        runtime.system.has_resource.return_value = True
        runtime.system.get_resource.return_value = resource
        runtime.device_registry.get = AsyncMock(return_value=None)
        runtime.topology.list_devices.return_value = []

        with pytest.raises(ZeroDivisionError):
            await adhoc.execute_adhoc_command(runtime, "dev_1", "shake")

        assert resource.take_calls == 1
        assert resource.release_calls == 1

    @patch("orca.gateway.adhoc.device_controller")
    @patch("orca.gateway.adhoc.device_connection_tracker")
    async def test_no_take_when_system_has_no_resource(
        self,
        mock_registry: MagicMock,
        mock_controller: MagicMock,
    ) -> None:
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(return_value=None)

        runtime = MagicMock()
        runtime.system.has_resource.return_value = False
        runtime.device_registry.get = AsyncMock(return_value=None)
        runtime.topology.list_devices.return_value = []

        result = await adhoc.execute_adhoc_command(runtime, "dev_1", "shake")

        assert result is not None
        runtime.system.has_resource.assert_called_once_with("dev_1")
        runtime.system.get_resource.assert_not_called()


@pytest.mark.asyncio
@patch("orca.gateway.adhoc.device_controller")
@patch("orca.gateway.adhoc.device_connection_tracker")
class TestAdhocEffectiveMode:
    """Which world an operator command dispatches into.

    The gateway controller refuses PURE_SIM before the wire, so the mode
    stamped here is the difference between an arm that moves and a
    ``400 invalid_command``. These cover the device the topology DECLARES,
    which is every bench device and the case the resolution actually has to
    combine an override into.
    """

    async def _dispatch(
        self, mock_registry: MagicMock, mock_controller: MagicMock, resource: Any,
    ) -> WorkflowRunMode:
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(return_value=None)

        await unseeded_await(adhoc.execute_adhoc_command(
            _runtime_declaring(resource), "dev_1", "shake",
        ))

        return mock_controller.execute_command.call_args.kwargs["effective_mode"]

    async def test_a_declared_device_with_no_override_reaches_the_hardware(
        self, mock_registry: MagicMock, mock_controller: MagicMock,
    ) -> None:
        """The bench case: a PF400 in topology, no override, operator presses
        move. Anything but LIVE here is the controller refusing the command."""
        mode = await self._dispatch(
            mock_registry, mock_controller, DeclaredDevice(),
        )

        assert mode is WorkflowRunMode.LIVE

    async def test_a_declared_device_in_sim_is_held_back_from_the_hardware(
        self, mock_registry: MagicMock, mock_controller: MagicMock,
    ) -> None:
        """An override is the operator's declaration that this device is not
        the real one, and an ad-hoc command must not override the override."""
        mode = await self._dispatch(
            mock_registry, mock_controller,
            DeclaredDevice(WorkflowRunMode.DEVICE_SIM),
        )

        assert mode is WorkflowRunMode.DEVICE_SIM

    async def test_a_device_pinned_pure_sim_never_reaches_the_wire(
        self, mock_registry: MagicMock, mock_controller: MagicMock,
    ) -> None:
        """PURE_SIM is the one mode the controller rejects, and a device that
        declares it should be exactly the device that gets rejected."""
        mode = await self._dispatch(
            mock_registry, mock_controller,
            DeclaredDevice(WorkflowRunMode.PURE_SIM),
        )

        assert mode is WorkflowRunMode.PURE_SIM

    async def test_a_command_inside_an_execution_follows_the_submission(
        self, mock_registry: MagicMock, mock_controller: MagicMock,
    ) -> None:
        """When there IS a submission its mode is the base, so an operator
        command issued under a sim run does not quietly go live."""
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(return_value=None)

        token = current_run_mode.set(WorkflowRunMode.DEVICE_SIM)
        try:
            await adhoc.execute_adhoc_command(
                _runtime_declaring(DeclaredDevice()), "dev_1", "shake",
            )
        finally:
            current_run_mode.reset(token)

        assert mock_controller.execute_command.call_args.kwargs[
            "effective_mode"
        ] is WorkflowRunMode.DEVICE_SIM


@pytest.mark.asyncio
@patch("orca.gateway.adhoc.device_controller")
@patch("orca.gateway.adhoc.device_connection_tracker")
class TestAdhocWithNoRuntime:
    """An ad-hoc command while the runtime is down.

    A failed rebuild tears the runtime down and leaves it that way until an
    operator submits a fix, so this is a state a deployment sits in, not a
    race. The device stays reachable throughout, because connections live in
    the process-global tracker rather than in the runtime: `resolve_device`
    still finds it and the request runs normally.

    What is NOT readable is the topology, and the `sim_override` in it is the
    only thing standing between an operator command and a real instrument.
    """

    async def test_a_command_is_refused_rather_than_dispatched_on_a_guess(
        self, mock_registry: MagicMock, mock_controller: MagicMock,
    ) -> None:
        """The device the operator names may be one declared DEVICE_SIM. With
        the declaration unreadable there is no way to tell, and dispatching
        LIVE to find out moves the arm it was meant to protect."""
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(return_value=None)

        with pytest.raises(ModeUnresolvableError) as exc:
            await unseeded_await(
                adhoc.execute_adhoc_command(None, "dev_1", "shake")
            )

        assert "dev_1" in str(exc.value)
        mock_controller.execute_command.assert_not_awaited()

    async def test_a_connected_gateway_only_device_still_dispatches(
        self, mock_registry: MagicMock, mock_controller: MagicMock,
    ) -> None:
        """The refusal is about an unreadable topology, not a missing entry in
        a readable one. A device the running system declares nothing about has
        no override to lose, so an operator command still reaches it."""
        mock_registry.get_device = AsyncMock(return_value=_snapshot())
        mock_controller.execute_command = AsyncMock(return_value=None)
        runtime = MagicMock()
        runtime.system.has_resource.return_value = False
        runtime.device_registry.get = AsyncMock(return_value=None)
        runtime.topology.list_devices.return_value = []

        await unseeded_await(
            adhoc.execute_adhoc_command(runtime, "dev_1", "shake")
        )

        assert mock_controller.execute_command.call_args.kwargs[
            "effective_mode"
        ] is WorkflowRunMode.LIVE
