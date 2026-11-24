"""A standalone method saves the same operation records as the same method in a workflow, deck bookkeeping included."""

import pytest

from tests.sdk_features.test_a_standalone_method_matches_the_workflow import (
    in_a_workflow_and_standalone,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


async def test_the_standalone_run_saves_the_same_operation_records() -> None:
    in_a_workflow, standalone = await in_a_workflow_and_standalone()

    assert standalone[2] == in_a_workflow[2]
