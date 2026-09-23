"""Device factory context contract: SDK no-driver constructor flow.

Verifies the contract that lets deployment-package authors write
`Shaker(name="x")` with no driver argument:

- When a factory is bound via `use_device_factory(...)`, Device subclasses
  consult `factory.build_drivers(device_type, name)` to obtain
  (live_driver, sim_driver) and wire them in.
- When no factory is bound, a fresh sim driver is created directly for
  both slots (pure-sim fallback for standalone users with no hosted deployment).
- Nested `use_device_factory` contexts stack and restore correctly.
- The contextvar is per-async-task so concurrent builds don't interfere.

Tests use `Shaker` because it's the smallest concrete subclass; the
behavior is inherited from `Device.__init__` and applies uniformly to
every subclass.
"""

import asyncio
from collections.abc import Callable

import pytest

from cheshire_drivers.interfaces import BaseDriver
from cheshire_drivers.sims import SimShakerDriver
from orca.devices.shaker import Shaker
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.device_factory_context import (
    use_device_factory,
)


def _open_constructor(ctor: Callable[..., Shaker]) -> Callable[..., Shaker]:
    """Erase the static signature so a retired-kwarg call reaches runtime arg checking."""
    return ctor


class _RecordingFactory:
    """IDeviceDriverProvider test double that records calls and returns prebuilt drivers.

    `build_drivers` returns the (live, sim) pair stored under the given
    device_type. Tests pre-load the map and assert (a) the right key was
    asked for and (b) the device received those exact instances.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._drivers: dict[str, tuple[BaseDriver, BaseDriver]] = {}

    def preload(self, device_type: str, live: BaseDriver, sim: BaseDriver) -> None:
        self._drivers[device_type] = (live, sim)

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[BaseDriver, BaseDriver]:
        self.calls.append((device_type, name))
        return self._drivers[device_type]


def test_no_factory_bound_defaults_to_sim_driver() -> None:
    shaker = Shaker(name="shaker_1")

    assert isinstance(shaker.driver, SimShakerDriver)
    # Sim and live slots both point at sim drivers in pure-sim default.
    assert isinstance(shaker._sim_manager._sim_driver, SimShakerDriver)


def test_factory_supplies_drivers_when_bound() -> None:
    factory = _RecordingFactory()
    live = SimShakerDriver("shaker_1_live")  # any concrete driver works as a stand-in
    sim = SimShakerDriver("shaker_1_sim")
    factory.preload("shaker", live, sim)

    with use_device_factory(factory):
        shaker = Shaker(name="shaker_1")

    assert factory.calls == [("shaker", "shaker_1")]
    # The live slot is _live_driver; the sim slot is _sim_driver.
    assert shaker._sim_manager._live_driver is live
    assert shaker._sim_manager._sim_driver is sim


def test_explicit_driver_kwarg_raises() -> None:
    """Regression: the explicit-driver path is permanently retired.

    Replaces the deleted ``test_explicit_driver_bypasses_factory``. Locks
    the no-driver-only contract into the test suite so a future revert
    would break loud rather than silently re-enabling backward compat.
    """
    with pytest.raises(TypeError):
        _open_constructor(Shaker)(name="x", driver=SimShakerDriver("x"))


def test_explicit_sim_driver_kwarg_raises() -> None:
    """Same regression for the previously-paired ``sim_driver=`` kwarg."""
    with pytest.raises(TypeError):
        _open_constructor(Shaker)(name="x", sim_driver=SimShakerDriver("x"))


def test_nested_factory_contexts_stack_and_restore() -> None:
    outer = _RecordingFactory()
    inner = _RecordingFactory()
    outer.preload("shaker", SimShakerDriver("outer_live"), SimShakerDriver("outer_sim"))
    inner.preload("shaker", SimShakerDriver("inner_live"), SimShakerDriver("inner_sim"))

    with use_device_factory(outer):
        Shaker(name="outer_a")
        with use_device_factory(inner):
            Shaker(name="inner_only")
        Shaker(name="outer_b")

    assert outer.calls == [("shaker", "outer_a"), ("shaker", "outer_b")]
    assert inner.calls == [("shaker", "inner_only")]


def test_factory_context_is_per_async_task() -> None:
    """Two concurrent tasks each binding a different factory must not interleave."""
    factory_a = _RecordingFactory()
    factory_b = _RecordingFactory()
    factory_a.preload("shaker", SimShakerDriver("a_live"), SimShakerDriver("a_sim"))
    factory_b.preload("shaker", SimShakerDriver("b_live"), SimShakerDriver("b_sim"))

    async def build_a() -> None:
        with use_device_factory(factory_a):
            await asyncio.sleep(0)
            Shaker(name="device_a")

    async def build_b() -> None:
        with use_device_factory(factory_b):
            await asyncio.sleep(0)
            Shaker(name="device_b")

    async def go() -> None:
        await asyncio.gather(build_a(), build_b())

    asyncio.run(go())

    assert factory_a.calls == [("shaker", "device_a")]
    assert factory_b.calls == [("shaker", "device_b")]


def test_sim_device_factory_implements_build_drivers() -> None:
    """The source-available SimDeviceFactory must satisfy the build_drivers contract."""
    factory = SimDeviceFactory()

    live, sim = factory.build_drivers("shaker", "shaker_1")

    assert isinstance(live, SimShakerDriver)
    assert isinstance(sim, SimShakerDriver)
    # Source-available sim factory uses the same class for both slots: there is no live
    # driver in pure-sim, just two sim instances feeding the SimulationManager.
    assert live is not sim


def test_sim_device_factory_rejects_unknown_device_type() -> None:
    factory = SimDeviceFactory()

    with pytest.raises(ValueError, match="Unknown device type"):
        factory.build_drivers("not_a_device_type", "foo")
