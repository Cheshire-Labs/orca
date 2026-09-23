"""`where` is a required field on the insert wire models.

A body that omits `where` must fail validation (a 422 on the REST/MCP surface)
rather than silently defaulting to "tail" -- insert placement is
position-sensitive, so callers must choose explicitly.
"""

import pytest
from pydantic import ValidationError

from orca.operations.thread_models import InsertActionRequest, InsertMethodRequest


def test_insert_method_request_requires_where() -> None:
    with pytest.raises(ValidationError) as exc:
        InsertMethodRequest(
            execution_id="e1", thread_id="t1", template_name="m1", reason="r",
        )
    assert any(e["loc"] == ("where",) for e in exc.value.errors())


def test_insert_action_request_requires_where() -> None:
    with pytest.raises(ValidationError) as exc:
        InsertActionRequest(
            execution_id="e1", thread_id="t1",
            action_code="@orca.action\ndef a(): pass\n", reason="r",
        )
    assert any(e["loc"] == ("where",) for e in exc.value.errors())


def test_insert_method_request_accepts_explicit_where() -> None:
    req = InsertMethodRequest(
        execution_id="e1", thread_id="t1", template_name="m1",
        where="head", reason="r",
    )
    assert req.where == "head"


def test_insert_request_rejects_unknown_where() -> None:
    with pytest.raises(ValidationError):
        InsertMethodRequest(
            execution_id="e1", thread_id="t1", template_name="m1",
            where="middle", reason="r",
        )
