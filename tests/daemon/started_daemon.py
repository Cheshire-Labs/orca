"""An in-process client to a daemon whose startup has run, as `orca start` runs it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@asynccontextmanager
async def started_daemon(app: FastAPI, base_url: str = "http://daemon.test") -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as client:
            yield client
