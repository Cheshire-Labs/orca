"""Base class for Orca plugins.

OrcaPlugin is the foundation for extending Orca with custom behavior.
Plugins receive RuntimeEvents from the SystemEventBus (all executions)
and can read the live system via self.system. A plugin offers no commands.
A plugin is a watcher: one that raises is logged, and the other plugins and
the code that emitted the event carry on.

Examples of plugins:
    - Notifications: webhook calls, email alerts on errors
    - Metrics: step timings sent to a monitoring system

Writing a plugin:

    from orca.plugins.base import OrcaPlugin
    from orca.events.runtime_event import RuntimeEvent
    from orca.events.execution_context import ThreadExecutionContext

    class MyPlugin(OrcaPlugin):
        def handle_runtime_event(self, event: RuntimeEvent) -> None:
            if isinstance(event.context, ThreadExecutionContext):
                print(f"Thread {event.context.thread_name}: {event.event_name}")

    # Usage:
    plugin = MyPlugin()
    runtime.register_plugin(plugin)
    # self.system is auto-injected when the plugin is registered
"""

from orca.events.event_handlers import SystemBoundEventHandler
from orca.events.runtime_event import RuntimeEvent


class OrcaPlugin(SystemBoundEventHandler):
    """Base class for all Orca plugins.

    Plugins receive all RuntimeEvents across all executions via
    handle_runtime_event(). Use event.execution_id to distinguish
    between concurrent workflows if needed.

    self.system is available for querying or interacting with the live
    system (threads, devices, workflows, etc.).
    """

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        """Called for every RuntimeEvent across all executions. Override to react."""
        ...
