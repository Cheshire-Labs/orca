"""The CLI reads a response that carries the blocker list as the Operation's model plus blockers, strictly."""

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from orca.cli.client import _parse_with_blockers


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str


_BLOCKERS: dict[str, JsonValue] = {
    "blockers": [], "blocker_count": 0, "blockers_known": True, "blockers_fingerprint": "f",
}


def _response(body: dict[str, JsonValue]) -> httpx.Response:
    return httpx.Response(200, json=body)


def test_a_response_with_its_blockers_reads_as_the_model_and_the_blockers() -> None:
    answer, blockers = _parse_with_blockers(_Answer, _response({"status": "ok", **_BLOCKERS}))

    assert answer == _Answer(status="ok")
    assert blockers.blockers_known is True


def test_a_missing_blocker_field_is_refused() -> None:
    body = {"status": "ok", **_BLOCKERS}
    del body["blocker_count"]

    with pytest.raises(ValidationError):
        _parse_with_blockers(_Answer, _response(body))


def test_a_renamed_blocker_field_is_refused() -> None:
    body = {"status": "ok", **_BLOCKERS}
    body["blockers_are_known"] = body.pop("blockers_known")

    with pytest.raises(ValidationError):
        _parse_with_blockers(_Answer, _response(body))
