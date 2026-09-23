"""Default logging setup for SDK scripts.

Scripts using ``orca.build_system()`` get a sensible console handler on the
``orca`` logger so messages land on stdout without the user wiring
``logging.basicConfig`` themselves. Users can override via the
``configure_logging`` parameter on ``build_system()``.
"""

import logging
import sys


_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def install_default_console_logging(level: int = logging.INFO) -> None:
    """Attach a stdout StreamHandler to the ``orca`` logger if it has none.

    No-op when the ``orca`` logger (or root) already has handlers installed,
    so repeated calls (e.g. test suites building many systems) stay idempotent
    and we never fight a caller who has configured logging themselves.
    """
    orca_logger = logging.getLogger("orca")
    if orca_logger.handlers or logging.getLogger().handlers:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    handler.setLevel(level)
    orca_logger.addHandler(handler)
    orca_logger.setLevel(level)
