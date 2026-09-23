"""Example plugin: read-only window onto the system's ops history.

This plugin exists primarily as a template for users writing their own
plugins. It does not own state; it reads from ``system.ops_history``
(the single source of truth owned by ISystem) and exposes a shell
command for inspecting the history at runtime.

Plugins ARE NOT a place to hold load-bearing system state. Anything the
workflow engine depends on (e.g. ops history) lives on ISystem so the
system can function with zero plugins registered. Plugins observe and
extend; they don't gate execution.

Authoring your own plugin:

    from orca.plugins.base import OrcaPlugin, PluginCommand
    from orca.events.runtime_event import RuntimeEvent

    class MyPlugin(OrcaPlugin):
        def handle_runtime_event(self, event: RuntimeEvent) -> None:
            # react to events -- log, notify, track stats, call webhooks, etc.
            ...

        def get_commands(self) -> list[PluginCommand]:
            return [PluginCommand("mycmd", "What it does", "mycmd [args]", self._handle)]

        async def _handle(self, args: list[str]) -> str:
            # self.system gives you the live ISystem, including ops_history,
            # tracking_context, variable_store, system_map, etc.
            return str(await self.system.ops_history.all_operations())

Register via ``runtime.register_plugin(MyPlugin())``. Plugins are
optional and swap freely without touching core orchestration.
"""
from orca.plugins.base import OrcaPlugin, PluginCommand


class ActionTracker(OrcaPlugin):
    """Shell command 'ops' to inspect the system's ops history.

    Registering this plugin adds no state and no event subscriptions --
    it's a thin convenience for debugging. Delete or replace freely.
    """

    async def _handle_ops_command(self, args: list[str]) -> str:
        history = self.system.ops_history
        if not args:
            names = sorted({
                lw for op in await history.all_operations() for lw in op.affected_labware
            })
            return "Tracked labware: " + (", ".join(names) or "(none)")
        labware = args[0]
        ops = await history.ops_for(labware)
        if not ops:
            return f"No ops recorded for {labware}"
        lines = [
            f"  {op.operation.value} ({op.details.__class__.__name__}) @ t={op.timestamp:.2f}"
            for op in ops
        ]
        return f"{labware} ({len(ops)} ops):\n" + "\n".join(lines)

    def get_commands(self) -> list[PluginCommand]:
        return [PluginCommand("ops", "Query ops history", "ops [labware_name]", self._handle_ops_command)]
