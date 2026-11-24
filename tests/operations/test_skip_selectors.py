"""Skip-method names its method by name only; a refused skip-action selector names the fields it takes."""
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from orca.operations.protocol import OperationError
from orca.operations.thread import SkipActionOperation
from orca.operations.thread_models import SkipActionRequest, SkipMethodRequest
from orca.runtime.runtime_interface import ISystemRuntime


def test_a_skip_method_body_that_sends_method_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="method_id"):
        SkipMethodRequest.model_validate(
            {"execution_id": "e1", "thread_id": "t1", "method_name": "m", "method_id": "m", "reason": "r"},
        )


def test_a_skip_method_body_without_a_method_name_is_refused() -> None:
    with pytest.raises(ValidationError, match="method_name"):
        SkipMethodRequest.model_validate({"execution_id": "e1", "thread_id": "t1", "reason": "r"})


@pytest.mark.parametrize(
    ("action_id", "action_command", "says"),
    [
        (None, None, "provide exactly one of action_id or action_command"),
        ("a1", "shake", "provide ONLY one of action_id or action_command, not both"),
    ],
)
async def test_a_refused_skip_action_selector_names_action_id_and_action_command(
    action_id: str | None, action_command: str | None, says: str,
) -> None:
    runtime = MagicMock(spec=ISystemRuntime)
    request = SkipActionRequest(
        execution_id="e1", thread_id="t1", action_id=action_id, action_command=action_command, reason="r",
    )
    with pytest.raises(OperationError) as refused:
        await SkipActionOperation(runtime).run(request)
    assert (refused.value.surface_code(), refused.value.message) == ("validation_error", says)
