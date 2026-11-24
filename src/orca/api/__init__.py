"""The public package for serving Orca's REST API."""

from orca.api.errors import ErrorEnvelope, error_response, install_error_handlers
from orca.api.event_feed import EventFeed
from orca.api.health import health_router
from orca.api.generic_catalog import GenericOperationCatalog, check_run_holding, with_runtime
from orca.api.interfaces import (
    IApiAuth,
    IApiContext,
    IEventFeed,
    IEventStream,
    IEventSubscriber,
    IOperationCatalog,
    OperationEntry,
    WebSocketRefusal,
)
from orca.api.mount import (
    DRIVES_DEVICE,
    EVENT_STREAM_PATH,
    LONG_POLL,
    drives_a_device,
    mount_api,
    mount_event_stream,
    never_timed_out,
    to_http,
)

__all__ = [
    "DRIVES_DEVICE",
    "EVENT_STREAM_PATH",
    "ErrorEnvelope",
    "EventFeed",
    "GenericOperationCatalog",
    "IApiAuth",
    "IApiContext",
    "IEventFeed",
    "IEventStream",
    "IEventSubscriber",
    "IOperationCatalog",
    "LONG_POLL",
    "OperationEntry",
    "WebSocketRefusal",
    "check_run_holding",
    "drives_a_device",
    "error_response",
    "health_router",
    "install_error_handlers",
    "mount_api",
    "mount_event_stream",
    "never_timed_out",
    "to_http",
    "with_runtime",
]
