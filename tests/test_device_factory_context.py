"""Device factory context contract: SDK no-driver constructor flow.

Verifies the contract that lets deployment-package authors write
`Shaker(name="x")` with no driver argument:

- When a factory is bound via `use_device_factory(...)`, Device subclasses
  consult `factory.build_driver(device_type, name)` for their one driver.
- With no factory bound, building a device is refused.
- A factory that builds the wrong kind of driver is refused.
- Nested `use_device_factory` contexts stack and restore correctly.
- The contextvar is per-async-task so concurrent builds don't interfere.

Tests use `Shaker` because it's the smallest concrete subclass; the
behavior is inherited from `Device.__init__` and applies uniformly to
every subclass.
"""

import asyncio
import contextvars
from collections.abc import Callable

import pytest

from cheshire_drivers.interfaces import BaseDriver
from cheshire_drivers.sims import SimShakerDriver, SimStorageDriver
from orca.devices.shaker import Shaker
from orca.driver_management.device_factory_context import NoDeviceFactoryError, use_device_factory


def _open_constructor(ctor: Callable[..., Shaker]) -> Callable[..., Shaker]:
    """Erase the static signature so a retired-kwarg call reaches runtime arg checking."""
    return ctor


class _RecordingFactory:
    """IDeviceDriverProvider test double that records calls and returns prebuilt drivers.

    `build_driver` returns the driver stored under the given device_type. Tests pre-load the map and assert (a) the right key was
    asked for and (b) the device received those exact instances.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._drivers: dict[str, BaseDriver] = {}

    def preload(self, device_type: str, driver: BaseDriver) -> None:
        self._drivers[device_type] = driver

    def build_driver(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> BaseDriver:
        self.calls.append((device_type, name))
        return self._drivers[device_type]


def test_a_device_built_with_no_factory_bound_is_refused() -> None:
    with pytest.raises(NoDeviceFactoryError, match="shaker_1"):
        contextvars.Context().run(Shaker, "shaker_1")


def test_the_refusal_says_how_to_run_a_device_and_names_no_internal_helper() -> None:
    with pytest.raises(NoDeviceFactoryError) as refused:
        contextvars.Context().run(Shaker, "shaker_1")

    message = str(refused.value)
    assert "topology.py" in message
    assert "orca topology mount" in message
    assert "README" in message
    assert "use_device_factory" not in message
    assert "factory" not in message


def test_a_factory_that_builds_the_wrong_kind_of_driver_is_refused() -> None:
    factory = _RecordingFactory()
    factory.preload("shaker", SimStorageDriver("shaker_1"))

    with use_device_factory(factory), pytest.raises(TypeError, match="IShakerDriver"):
        Shaker(name="shaker_1")


def test_factory_supplies_drivers_when_bound() -> None:
    factory = _RecordingFactory()
    driver = SimShakerDriver("shaker_1")  # any concrete driver works as a stand-in
    factory.preload("shaker", driver)

    with use_device_factory(factory):
        shaker = Shaker(name="shaker_1")

    assert factory.calls == [("shaker", "shaker_1")]
    assert shaker.driver is driver


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
    outer.preload("shaker", SimShakerDriver("outer"))
    inner.preload("shaker", SimShakerDriver("inner"))

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
    factory_a.preload("shaker", SimShakerDriver("a"))
    factory_b.preload("shaker", SimShakerDriver("b"))

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
