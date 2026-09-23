"""WorkflowContext: the builder object passed to @orca.workflow functions.

Provides methods to declare workflow structure: entry threads, auto-spawn
registrations, event handlers, and variables.
"""

from orca.events.event_bus_interface import EventHandlerType
from orca.resource_models.capacity import CapacityPolicy
from orca.resource_models.labware_state import IOverflowStrategy
from orca.variables.variable_definition import VariableDefinition
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate


class WorkflowContext:
    """Builder passed to @orca.workflow decorated functions.

    Usage::

        @orca.workflow(name="my_assay")
        def my_assay(wf):
            wf.start(plate_thread)
            wf.thread(tips_thread)
            wf.on("THREAD.CREATED", MyHandler())
    """

    def __init__(self, template: WorkflowTemplate) -> None:
        self._template = template

    def start(self, thread: ThreadTemplate) -> None:
        """Register an entry thread that starts when the workflow starts."""
        self._template.add_thread(thread, is_start=True)

    def thread(self, thread: ThreadTemplate,
               capacity: CapacityPolicy | None = None,
               overflow_strategy: IOverflowStrategy | None = None) -> None:
        """Register a contributor thread for auto-spawning.

        When an action needs labware owned by this thread, the engine
        spawns a fresh instance automatically. The thread's generator
        should yield orca.join() at the point where it participates
        in the shared action.

        capacity declares slot-level CapacityPolicy. overflow_strategy
        attaches a custom IOverflowStrategy for non-default overflow
        handling (priority queues, parallel receivers, etc.).
        """
        self._template.add_thread(thread, is_start=False)
        self._template.register_auto_spawn(
            thread, capacity=capacity, overflow_strategy=overflow_strategy,
        )

    def on(self, event_name: str, handler: EventHandlerType) -> None:
        """Subscribe an event handler to a workflow-level event."""
        self._template.add_event_handler(event_name, handler)

    def variable(self, name: str, definition: VariableDefinition) -> None:
        """Add a workflow-scoped variable definition."""
        self._template.add_variable(name, definition)

    @property
    def template(self) -> WorkflowTemplate:
        return self._template
