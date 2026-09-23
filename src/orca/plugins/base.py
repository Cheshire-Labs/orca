"""Base class for Orca plugins.

OrcaPlugin is the foundation for extending Orca with custom behavior.
Plugins receive RuntimeEvents from the SystemEventBus (all executions)
and can interact with the live system via self.system.

Examples of plugins:
    - Tracking: method progress, labware journeys, timing metrics
    - Notifications: webhook calls, email alerts on errors
    - Frontend backends: serve live status to a custom GUI
    - AI integration: feed events to an AI agent for decision-making
    - Variable control: set runtime variables based on sensor data

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

from dataclasses import dataclass
from typing import Awaitable, Callable

from orca.events.event_handlers import SystemBoundEventHandler
from orca.events.runtime_event import RuntimeEvent


@dataclass(frozen=True)
class PluginCommand:
    """A command that a plugin exposes to CLI/MCP/REST interfaces.

    The handler is async so plugins can read OpsHistory or other async
    system facades; the dispatcher (``SystemRuntime.execute_plugin_command``)
    awaits the result and returns its string body to the caller.
    """

    name: str
    description: str
    usage: str
    handler: Callable[[list[str]], Awaitable[str]]


class OrcaPlugin(SystemBoundEventHandler):
    """Base class for all Orca plugins.

    Plugins receive all RuntimeEvents across all executions via
    handle_runtime_event(). Use event.execution_id to distinguish
    between concurrent workflows if needed.

    self.system is available for querying or interacting with the live
    system (threads, devices, workflows, etc.).

    Override get_commands() to expose plugin-specific commands to
    CLI/MCP/REST interfaces.
    """

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        """Called for every RuntimeEvent across all executions. Override to react."""
        ...

    def get_commands(self) -> list[PluginCommand]:
        """Return commands this plugin exposes to CLI/MCP/REST. Default: none."""
        return []
