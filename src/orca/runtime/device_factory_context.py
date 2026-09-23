"""Active device factory binding for the no-driver SDK constructor flow.

The deployment-package author writes::

    shaker = Shaker(name="ml_star")  # no driver argument

Each Device subclass's `__init__` consults `_get_active_factory()`. When a
factory is bound (via `use_device_factory(factory)`), the subclass calls
`factory.build_drivers(device_type, name)` and wires the returned
(live_driver, sim_driver) pair into `SimulationManager`. When no factory
is bound, the subclass falls back to constructing a sim driver directly
for both slots so standalone pure-sim runs work without any binding.

The binding is held in a `contextvars.ContextVar`, which means:

- `use_device_factory(...)` is a context manager that pushes a new value
  for the duration of the `with` block and restores the previous value
  on exit.
- Asyncio tasks each get their own copy of the ContextVar, so
  concurrent topology builds in different tasks don't interfere.
- Nested bindings stack and restore in LIFO order.

A hosted deployment's `RuntimeLifecycle` wraps the deployment-package import in
`with use_device_factory(remote_factory)` so every device constructed
inside `system.py:build()` resolves drivers through `RemoteDeviceFactory`.
Standalone pure-sim entry points either (a) skip the binding entirely and let
the subclass default to sim drivers, or (b) bind a `SimDeviceFactory`
explicitly when they want the consistency of going through the factory
contract.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, TypeVar, cast

from orca.runtime.device_factory_protocol import IDeviceDriverProvider


# The driver TypeVar is intentionally unbounded: orca-core's driver
# interfaces (`BaseDriver`, `ITransporterDriver`, ...) are sibling ABCs,
# not a single hierarchy. `resolve_drivers` is structurally polymorphic
# over the driver type its caller is wiring; the only contract is that
# the explicit-driver, explicit-sim, and default_sim_factory arguments
# all share the same type.
TDriver = TypeVar("TDriver")


_active_factory: ContextVar[IDeviceDriverProvider | None] = ContextVar(
    "_active_device_factory", default=None,
)


def _get_active_factory() -> IDeviceDriverProvider | None:
    """Return the currently-bound driver provider, or None if unbound.

    Used by Device subclasses' `__init__` when no `driver` argument was
    supplied. None means "no factory bound; fall back to sim default".
    """
    return _active_factory.get()



def is_factory_bound() -> bool:
    """Return True when a device factory is currently bound on the contextvar.

    Public alternative to peeking at the underscore-prefixed
    ``_get_active_factory()``. Use to layer a default factory only when no
    outer factory is in scope (e.g., an example topology that wants to
    bind ChatterboxLiquidHandlerDriver for the LH unless a hosted
    deployment or a test has already bound something else).
    """
    return _active_factory.get() is not None


@contextmanager
def use_device_factory(factory: IDeviceDriverProvider) -> Iterator[None]:
    """Bind `factory` as the active device factory for the duration of the block.

    `factory` only needs to satisfy `IDeviceDriverProvider` (`build_drivers`);
    the resolution path here consults nothing else.

    Stacks correctly when nested: the previous value is restored on exit.
    Per-task in async contexts: tasks see their own ancestor binding,
    not each other's.

    Typical use::

        with use_device_factory(remote_factory):
            module = importlib.import_module("deployment_package.system")
            build = module.build(stores)  # devices construct here

    """
    token = _active_factory.set(factory)
    try:
        yield
    finally:
        _active_factory.reset(token)


def resolve_drivers(
    device_type: str,
    name: str,
    default_sim_factory: Callable[[str], TDriver],
    *,
    deck_modeling: bool = False,
) -> tuple[TDriver, TDriver]:
    """Resolve the (live, sim) driver pair for a Device subclass `__init__`.

    Precedence (highest first):

    1. **Bound factory**: `use_device_factory(...)` is active.
       `factory.build_drivers(device_type, name)` supplies both slots.
       Used by a hosted deployment's `RuntimeLifecycle` build path so
       deployment-package authors writing `Shaker(name="x")` get
       `(RemoteShakerDriver, SimShakerDriver)` automatically. Tests
       that need to inject a specific driver instance bind a custom
       provider through `use_device_factory(...)` for the duration of
       the topology build.
    2. **Pure-sim default**: no factory bound. Two fresh
       `default_sim_factory(name)` instances. Standalone users running
       without a hosted deployment get a working sim build with zero ceremony.

    `deck_modeling` flows to both paths so a deck-modeling
    ``LiquidHandler`` gets a deck-modeling sim slot (real Chatterbox
    deck) while a deckless ``LiquidHandlerProtocol`` keeps the no-op
    protocol sim. The bound factory reads it in `build_drivers`; the
    pure-sim path expects the caller to have chosen `default_sim_factory`
    accordingly, so the flag is forwarded only to the factory.

    The flag is forwarded to `build_drivers` ONLY when True, so a factory
    that does not model decks (every non-LH device, deckless handlers, and
    test doubles) sees the historical two-argument call and needs no
    `deck_modeling` parameter. A factory that builds a deck-modeling
    ``LiquidHandler`` must accept the keyword.

    There is no explicit-driver path. Device subclass constructors do
    not accept `driver=` / `sim_driver=` kwargs; tests inject through
    the factory contextvar. The TypeVar `TDriver` ties the call site's
    sim-factory type to the cast applied to the factory's return.
    """
    factory = _get_active_factory()
    if factory is not None:
        if deck_modeling:
            live, sim = factory.build_drivers(device_type, name, deck_modeling=True)
        else:
            live, sim = factory.build_drivers(device_type, name)
        return cast(TDriver, live), cast(TDriver, sim)
    return default_sim_factory(name), default_sim_factory(name)
