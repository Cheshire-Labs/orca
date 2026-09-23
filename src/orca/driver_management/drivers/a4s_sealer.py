from typing import ClassVar

from cheshire_drivers.interfaces import ISealerDriver
from cheshire_drivers.sims import SimSealerDriver
from cheshire_drivers.plr import A4SSealerDriver
from orca.resource_models.devices import Device


class A4SSealer(Device[ISealerDriver]):
    KIND: ClassVar[str] = "sealer"

    def __init__(
        self,
        name: str,
        port: str,
        timeout: int | None = None,
    ):
        """``timeout`` is how long the driver waits on the serial link before giving up.

        Leave it unset. The driver then derives a budget that outlasts every
        command it declares, so an overrunning seal reaches the engine's abort and
        the operator gets the recoverable-timeout decision. The 20s this used to
        hardcode sat under a `seal` the engine allows 600s, so a normal seal cycle
        came back as a transport error.
        """
        super().__init__(
            name,
            A4SSealerDriver(port=port, timeout=timeout),
            SimSealerDriver(name),
        )
