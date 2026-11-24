"""Where the daemon serves each remedy action. Actions it has no route for are left out."""

from orca.operations.blocker_wire import ActionRoute, route_of
from orca.operations.remedy_operations import REMEDY_OPERATIONS
from orca.operations.system_loading import MountPackageOperation

MOUNT_PACKAGE = "mount_package"

DAEMON_ACTION_ROUTES: dict[str, ActionRoute] = {
    **{action: route_of(op_class) for action, op_class in REMEDY_OPERATIONS.items()},
    MOUNT_PACKAGE: route_of(MountPackageOperation),
}
