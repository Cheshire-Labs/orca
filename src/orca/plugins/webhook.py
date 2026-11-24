"""WebhookPlugin: POST each matching RuntimeEvent as JSON to a URL, retrying with backoff, off the emit path."""

import asyncio
import logging

import httpx
from orca.events.runtime_event import RuntimeEvent

from orca.plugins.async_offloading import AsyncOffloadingPlugin
from orca.plugins.config import EventFilter

logger = logging.getLogger(__name__)

_STOP_DRAIN_TIMEOUT = 5.0


class WebhookPlugin(AsyncOffloadingPlugin):
    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        event_filter: EventFilter | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        queue_size: int = 256,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(queue_size=queue_size, stop_drain_timeout=_STOP_DRAIN_TIMEOUT)
        self._url = url
        self._headers = dict(headers or {})
        self._event_filter = event_filter
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        # Injected in tests (httpx.MockTransport); created lazily otherwise so
        # the connection pool binds to the drain task's running loop.
        self._client = client
        self._owns_client = client is None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def deliver(self, event: RuntimeEvent) -> None:
        if self._event_filter is not None and not self._event_filter.matches(event):
            return
        payload = event.to_dict()
        client = self._ensure_client()
        for attempt in range(self._max_retries):
            try:
                response = await client.post(
                    self._url, json=payload, headers=self._headers,
                )
                response.raise_for_status()
                return
            except Exception as exc:
                if attempt + 1 >= self._max_retries:
                    # Never log headers; they may carry a token.
                    logger.warning(
                        "webhook POST to %s failed after %d attempt(s): %s",
                        self._url, self._max_retries, exc,
                    )
                    return
                await asyncio.sleep(self._backoff_base * (2 ** attempt))

    async def stop(self) -> None:
        await super().stop()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
