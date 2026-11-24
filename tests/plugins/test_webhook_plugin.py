"""Unit tests for WebhookPlugin.

POSTs RuntimeEvent JSON to a configured URL off the emit path, applies the
event filter, sends configured headers, and retries with backoff on
transient failure.
"""

import time
from collections.abc import Awaitable, Callable

import httpx
import pytest
from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent

from orca.plugins.config import EventFilter
from orca.plugins.webhook import WebhookPlugin


pytestmark = pytest.mark.asyncio


def _make_event(
    entity_type: str = "INCIDENT", status: str = "RAISED",
) -> RuntimeEvent:
    return RuntimeEvent(
        event_name=f"{entity_type}.i-1.{status}",
        execution_id="exec-1",
        timestamp=time.time(),
        entity_type=entity_type,
        entity_id="i-1",
        status=status,
        context=WorkflowExecutionContext(
            execution_id="exec-1", workflow_name="wf",
        ),
    )


def _client_for(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_posts_event_json_to_url(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    plugin = WebhookPlugin(
        url="https://lims.example/hook",
        headers={"Authorization": "Bearer secret-token"},
        client=_client_for(handler),
    )
    plugin.start()

    event = _make_event()
    plugin.handle_runtime_event(event)
    await wait_until(lambda: len(captured) == 1)
    await plugin.stop()

    assert len(captured) == 1
    req = captured[0]
    assert str(req.url) == "https://lims.example/hook"
    assert req.headers["Authorization"] == "Bearer secret-token"
    body = req.read().decode()
    assert '"event_name":"INCIDENT.i-1.RAISED"' in body.replace(" ", "")


async def test_event_filter_skips_non_matching(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    plugin = WebhookPlugin(
        url="https://lims.example/hook",
        event_filter=EventFilter(entity_types=frozenset({"INCIDENT"})),
        client=_client_for(handler),
    )
    plugin.start()

    plugin.handle_runtime_event(_make_event(entity_type="thread", status="STARTED"))
    plugin.handle_runtime_event(_make_event(entity_type="INCIDENT", status="RAISED"))
    await wait_until(lambda: len(captured) == 1)
    await plugin.stop()

    assert len(captured) == 1
    assert captured[0].read().decode().replace(" ", "").count('"entity_type":"INCIDENT"') == 1


async def test_retries_then_succeeds(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200)

    plugin = WebhookPlugin(
        url="https://lims.example/hook",
        max_retries=3,
        backoff_base=0.01,
        client=_client_for(handler),
    )
    plugin.start()

    plugin.handle_runtime_event(_make_event())
    await wait_until(lambda: attempts["n"] == 3)
    await plugin.stop()

    assert attempts["n"] == 3


async def test_gives_up_after_max_retries_without_crashing(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500)

    plugin = WebhookPlugin(
        url="https://lims.example/hook",
        max_retries=2,
        backoff_base=0.01,
        client=_client_for(handler),
    )
    plugin.start()

    plugin.handle_runtime_event(_make_event())
    # First event exhausts its two attempts, then a second still delivers:
    # the drain loop survives a permanently-failing target.
    await wait_until(lambda: attempts["n"] == 2)
    plugin.handle_runtime_event(_make_event())
    await wait_until(lambda: attempts["n"] == 4)
    await plugin.stop()

    assert attempts["n"] == 4  # two events x two attempts each


async def test_connection_error_is_retried_then_survives(
    wait_until: Callable[..., Awaitable[None]],
) -> None:
    # A transport-level failure (timeout / connect error) is handled like an
    # HTTP error: retried, then given up on without killing the drain loop.
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    plugin = WebhookPlugin(
        url="https://lims.example/hook",
        max_retries=2,
        backoff_base=0.01,
        client=_client_for(handler),
    )
    plugin.start()

    plugin.handle_runtime_event(_make_event())
    await wait_until(lambda: attempts["n"] == 2)
    # The drain survived: a follow-up event is still attempted.
    plugin.handle_runtime_event(_make_event())
    await wait_until(lambda: attempts["n"] == 4)
    await plugin.stop()

    assert attempts["n"] == 4
