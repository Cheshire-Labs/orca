"""Each error code whose spelling or status changed, answered by one real route with its one status."""

from typing import Literal

import pytest
from httpx import AsyncClient
from pydantic import JsonValue

from orca.operations.error_codes import ErrorCode

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

_CASES: list[tuple[ErrorCode, Method, str, JsonValue]] = [
    (ErrorCode.ROUTE_NOT_FOUND, "GET", "/api/no-such-route", None),
    (ErrorCode.DEVICE_NOT_FOUND, "GET", "/api/devices/no_such_device/introspection", None),
    (ErrorCode.EXECUTION_NOT_FOUND, "GET", "/api/executions/nope", None),
    (ErrorCode.SUBMISSION_NOT_FOUND, "GET", "/api/submissions/nope", None),
    (ErrorCode.WORKFLOW_NOT_FOUND, "GET", "/api/workflows/nope", None),
    (ErrorCode.METHOD_NOT_FOUND, "GET", "/api/methods/nope", None),
    (ErrorCode.LABWARE_NOT_FOUND, "GET", "/api/labware-instances/nope", None),
    (ErrorCode.LABWARE_TYPE_NOT_FOUND, "GET", "/api/labware-catalog/nope", None),
    (ErrorCode.MOVER_NOT_FOUND, "POST", "/api/movers/nope/release-hold", {"reason": "test"}),
    (ErrorCode.HELD_COMMAND_NOT_FOUND, "POST", "/api/incidents/nope/recoverable-timeout/extend", {"additional_seconds": 1.0}),
    (ErrorCode.PROFILE_NOT_FOUND, "GET", "/api/profiles/nope", None),
    (ErrorCode.GRIP_PROFILE_NOT_FOUND, "DELETE", "/api/grip-profiles/nope", None),
    (ErrorCode.MOVE_DEFAULTS_NOT_FOUND, "DELETE", "/api/move-defaults/nope", None),
    (ErrorCode.THREAD_NOT_FOUND, "GET", "/api/executions/nope/threads/nope", None),
    (ErrorCode.LABWARE_NOT_FOUND, "POST", "/api/labware-instances/nope/edit-location",
     {"location": "nowhere", "reason": "test"}),
    (ErrorCode.LABWARE_NOT_FOUND, "POST", "/api/labware-instances/nope/reset-location",
     {"location": "nowhere", "reason": "test"}),
    (ErrorCode.EXECUTION_NOT_FOUND, "POST", "/api/executions/nope/threads",
     {"template_name": "nope", "reason": "test"}),
    (ErrorCode.VALIDATION_ERROR, "POST", "/api/devices/shaker1/commands", {}),
    (ErrorCode.VALIDATION_ERROR, "GET", "/api/events?tail=5&since=1", None),
    (ErrorCode.VALIDATION_ERROR, "GET", "/api/incidents?category=bogus", None),
    (ErrorCode.VALIDATION_ERROR, "PUT", "/api/access-configs/nope", {}),
]


@pytest.mark.parametrize(("code", "method", "path", "body"), _CASES, ids=[f"{c}-{p}" for c, _, p, _ in _CASES])
async def test_the_route_answers_the_code_with_its_status(
    client: AsyncClient, code: ErrorCode, method: Method, path: str, body: JsonValue,
) -> None:
    resp = await client.request(method, path, json=body)
    assert (resp.status_code, resp.json()["detail"]["code"]) == (code.http_status, code), resp.text
