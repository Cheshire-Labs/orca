"""A `required=False` thread that a group leaves out does not run, and the run completes without it."""

import pytest

from tests.sdk_features.test_an_optional_thread import run_with

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_a_group_that_leaves_the_optional_thread_out_runs_without_it() -> None:
    await run_with(("sample_journey",), [])
