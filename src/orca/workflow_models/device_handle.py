"""DeviceHandle and ActionRequest for code method queue bridge.

When a user calls ``await ctx.device("shaker_1").shake(30, 500)`` inside an
``@orca.method`` function, the DeviceHandle intercepts the call, creates an
ActionRequest, puts it on an asyncio.Queue, and blocks until the thread loop
executes the action and signals completion.

ActionRequest is the runtime equivalent of ActionTemplate. Both converge at
UnresolvedLocationAction -- ActionTemplate does it at factory time via
MethodActionFactory, ActionRequest does it at runtime inside
resolve_next_action().
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActionRequest:
    """Runtime message requesting a device action from within a code method."""
    device_name: str
    command: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    result: Any = None
    error: Exception | None = None


class DeviceHandle:
    """Captures device method calls and routes them through the queue bridge.

    Returned by ``ctx.device("name")``. Each attribute access returns an
    async callable that creates an ActionRequest, enqueues it, and awaits
    completion.
    """

    def __init__(self, device_name: str, action_queue: asyncio.Queue[ActionRequest | None]) -> None:
        self._device_name = device_name
        self._queue = action_queue

    def __getattr__(self, method_name: str) -> Any:
        async def handle_call(*args: Any, **kwargs: Any) -> Any:
            action = ActionRequest(
                device_name=self._device_name,
                command=method_name,
                args=args,
                kwargs=kwargs,
            )
            await self._queue.put(action)
            await action.completion.wait()
            if action.error is not None:
                raise action.error
            return action.result
        return handle_call
