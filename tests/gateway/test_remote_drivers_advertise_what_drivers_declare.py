"""A remote driver must advertise exactly what its cheshire-drivers interface
declares, or the wire contract silently rots.

The gap this pins: `ITransporterDriver` gained `IHomeable`, and orca's
`RemoteTransporterDriver` went on advertising `{"ITransporter"}` from a literal
of its own. Topology cards derive the declared set from the interface, so every
transporter became a card whose connection could not satisfy it, and
`assert_runnable` refused the device with a `TopologyCollisionError` before any
workflow ran. Nothing failed at import; it failed on the bench.

Each remote driver restates `interfaces` rather than inheriting it (Python
shadows the ClassVar instead of unioning, and drivers restates for the same
reason), so nothing but a check like this keeps the two in step.
"""

import pytest

from cheshire_drivers.interfaces import (
    ICentrifugeDriver,
    IDelidderDriver,
    ILiquidHandlerDriver,
    IReaderDriver,
    ISealerDriver,
    IShakerDriver,
    IThermocyclerDriver,
    ITransporterDriver,
)
from orca.gateway.registry.capabilities import NAME_TO_INTERFACE
from orca.gateway.remote_drivers import (
    RemoteCentrifugeDriver,
    RemoteDelidderDriver,
    RemoteLiquidHandlerDriver,
    RemoteReaderDriver,
    RemoteSealerDriver,
    RemoteShakerDriver,
    RemoteThermocyclerDriver,
)
from orca.gateway.remote_transporter_driver import RemoteTransporterDriver


# Only drivers whose remote stands for the WHOLE interface. The liquid handler
# is deliberately absent: it carries a coarse marker and sources its real facets
# per-instance from the connection card (see
# test_remote_lh_declared_interfaces).
WHOLE_INTERFACE_REMOTES = [
    (RemoteTransporterDriver, ITransporterDriver),
    (RemoteShakerDriver, IShakerDriver),
    (RemoteCentrifugeDriver, ICentrifugeDriver),
    (RemoteThermocyclerDriver, IThermocyclerDriver),
    (RemoteSealerDriver, ISealerDriver),
    (RemoteReaderDriver, IReaderDriver),
    (RemoteDelidderDriver, IDelidderDriver),
]


@pytest.mark.parametrize(
    "remote_cls,interface_cls",
    WHOLE_INTERFACE_REMOTES,
    ids=lambda c: c.__name__ if isinstance(c, type) else str(c),
)
def test_remote_advertises_its_interfaces_declared_set(
    remote_cls: type, interface_cls: type,
) -> None:
    assert remote_cls.interfaces == interface_cls.interfaces, (
        f"{remote_cls.__name__} advertises {sorted(remote_cls.interfaces)} but "
        f"{interface_cls.__name__} declares {sorted(interface_cls.interfaces)}. "
        f"A topology card built from the interface will not be satisfiable by a "
        f"connection built from the remote, and the device is refused at submit."
    )


@pytest.mark.parametrize(
    "remote_cls,interface_cls",
    [*WHOLE_INTERFACE_REMOTES, (RemoteLiquidHandlerDriver, ILiquidHandlerDriver)],
    ids=lambda c: c.__name__ if isinstance(c, type) else str(c),
)
def test_every_advertised_name_is_mappable(
    remote_cls: type, interface_cls: type,
) -> None:
    """An advertised name with no `NAME_TO_INTERFACE` entry cannot be resolved
    back to a class, so the capability gate drops it and the commands behind it
    read as unsupported."""
    del interface_cls
    unmapped = sorted(set(remote_cls.interfaces) - set(NAME_TO_INTERFACE))
    assert not unmapped, (
        f"{remote_cls.__name__} advertises {unmapped} which NAME_TO_INTERFACE "
        f"cannot resolve; add the interface there in the same change."
    )
