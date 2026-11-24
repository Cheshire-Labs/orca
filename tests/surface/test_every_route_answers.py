"""Every daemon route has a case that calls it against a running daemon in simulation."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue

from orca.api import DRIVES_DEVICE
from orca.daemon.app import create_app
from tests.surface.device_locks import locks_taken_by_the_last_request
from tests.surface.in_order import InOrder
from tests.surface.rest_cases import ALL_CASES
from tests.surface.worlds import Worlds, case_params


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Worlds]:
    opened = Worlds(tmp_path_factory.mktemp("rest"), paused_run=False)
    yield opened
    opened.close()


@pytest.fixture(scope="module")
def in_order() -> InOrder:
    return InOrder([c.world for c in ALL_CASES])


def _carries(body: JsonValue, status: int, expect: str) -> bool:
    if status == 204 or expect == "":
        return True
    if status >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        return isinstance(detail, dict) and detail.get("code") == expect
    if isinstance(body, list):
        return expect == "[]"
    return isinstance(body, dict) and expect in body


@pytest.fixture(scope="module")
def routes_that_drive_a_device() -> set[str]:
    paths = create_app().openapi()["paths"]
    return {
        f"{method.upper()} {path}" for path, ops in paths.items() for method, op in ops.items()
        if isinstance(op, dict) and op.get(DRIVES_DEVICE)
    }


@pytest.mark.parametrize("index", case_params([c.world for c in ALL_CASES]), ids=[c.case_id for c in ALL_CASES])
def test_the_route_answers(
    index: int, worlds: Worlds, in_order: InOrder, routes_that_drive_a_device: set[str],
) -> None:
    in_order.start(index)
    case = ALL_CASES[index]
    context = worlds.get(case.world)
    if case.before is not None:
        case.before(context)
    path = (case.path or case.template).format(**context.notes)
    body = case.body(context) if callable(case.body) else case.body
    params = {k: v.format(**context.notes) if isinstance(v, str) else v for k, v in (case.params or {}).items()}
    reply = context.world.request(case.method, path, json=body, params=params or None)
    typed_refusal = case.status == 502 and _carries(reply.body, 502, "command_error")
    took = locks_taken_by_the_last_request()
    assert not took or case.key in routes_that_drive_a_device, (
        f"{case.key} took {sorted(took)} but its Operation's binding does not declare drives_device=True, "
        "so a server may time it out part-way through a device command"
    )
    assert reply.status < 500 or typed_refusal, f"{case.key} answered {reply.status}: {reply.text[:300]}"
    assert reply.status == case.status, f"{case.key} answered {reply.status}: {reply.text[:300]}"
    assert _carries(reply.body, reply.status, case.expect), f"{case.key} answer lacks {case.expect!r}: {reply.text[:300]}"
    assert case.contains in reply.text, f"{case.key} answer lacks {case.contains!r}: {reply.text[:300]}"
    if case.after is not None:
        case.after(context, reply.body)
