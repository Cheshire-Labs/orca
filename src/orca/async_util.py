"""Event-loop utilities shared across layers (no orca imports)."""
import asyncio
from typing import Awaitable, Protocol, TypeVar

_T_co = TypeVar("_T_co", covariant=True)


class _DrainableWaiter(Awaitable[_T_co], Protocol[_T_co]):
    def done(self) -> bool: ...
    def cancel(self) -> bool: ...


async def drain_cancelled_waiters(
    *waiters: _DrainableWaiter[_T_co],
) -> list[_T_co | BaseException]:
    """Cancel and drain racer tasks after an event race concludes.

    Gather, never a per-waiter await: awaiting a cancelled loser raises
    CancelledError that is indistinguishable from the CALLER's own
    cancellation, so a per-waiter try/except swallows an operator abort
    and zombie-parks the thread. Callers discard the returned results.
    """
    for waiter in waiters:
        if not waiter.done():
            waiter.cancel()
    return await asyncio.gather(*waiters, return_exceptions=True)
