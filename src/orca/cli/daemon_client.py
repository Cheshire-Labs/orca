"""The verb only the local daemon has: build its package folder into a running system."""

from orca.cli.client import OrcaClient
from orca.operations.system_loading_models import MOUNT_PACKAGE_ROUTE
from orca.operations.wire_models import MountPackageRequest
from orca.runtime.runtime_state import RuntimeState


class DaemonLoadingClient(OrcaClient):
    """`OrcaClient` plus mounting, which only the local daemon serves."""

    def mount_package(self) -> RuntimeState:
        """Build the daemon's package folder and start the runtime."""
        answer = self._call(MOUNT_PACKAGE_ROUTE, MountPackageRequest())
        return answer.runtime_state
