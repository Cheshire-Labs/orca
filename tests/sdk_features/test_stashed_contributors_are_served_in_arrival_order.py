"""A contributor that arrives after a stashed one, once the receiver has finished, is served after it."""

import pytest

from tests.sdk_features.kit import close_world
from tests.sdk_features.test_stashed_overflow_is_served_in_order import (
    three_contributors_one_stashed,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


async def test_the_stashed_contributor_is_served_before_a_later_arrival() -> None:
    world, transfers, _ = await three_contributors_one_stashed()
    try:
        assert [sample for sample, _, _ in transfers.seen] == transfers.arrivals, "service order equals arrival order"
    finally:
        await close_world(world)
