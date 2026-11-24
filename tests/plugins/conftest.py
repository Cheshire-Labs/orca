import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

import pytest
from typing_extensions import ParamSpec

from tests.test_helpers import wait_until as _wait_until

_P = ParamSpec("_P")
_T = TypeVar("_T")


@pytest.fixture
def wait_until() -> Callable[..., Awaitable[None]]:
    return _wait_until


class CountingExecutor(ThreadPoolExecutor):
    """Default executor that counts what is handed to it, so a test can see an idle task holds no worker thread."""

    def __init__(self) -> None:
        super().__init__(max_workers=2)
        self.submissions = 0

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        self.submissions += 1
        return super().submit(fn, *args, **kwargs)


@pytest.fixture
async def counting_default_executor() -> AsyncIterator[CountingExecutor]:
    executor = CountingExecutor()
    asyncio.get_running_loop().set_default_executor(executor)
    # Not shut down: the loop still points at it, and closing the loop reaps it.
    yield executor
