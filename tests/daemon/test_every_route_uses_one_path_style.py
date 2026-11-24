"""Every daemon route uses kebab-case segments, and every operation named as a read is a GET."""

import re

import pytest

from orca.daemon.app import create_app

_SEGMENT = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*|\{[a-z_]+\}")
_READ = re.compile(r"(get|list|show)_")

Row = tuple[str, str, str]


@pytest.fixture(scope="module")
def routes() -> list[Row]:
    paths = create_app().openapi()["paths"]
    rows = [(method.upper(), path, op["operationId"]) for path, ops in paths.items() for method, op in ops.items()]
    assert len(rows) > 150
    return rows


def _paths_not_in_kebab_case(rows: list[Row]) -> list[str]:
    return [path for _, path, _ in rows if not all(_SEGMENT.fullmatch(s) for s in path.removeprefix("/").split("/"))]


def _reads_not_sent_as_get(rows: list[Row]) -> list[str]:
    return [f"{method} {path} ({op})" for method, path, op in rows if _READ.match(op) and method != "GET"]


def test_every_path_segment_is_kebab_case_or_a_parameter(routes: list[Row]) -> None:
    assert _paths_not_in_kebab_case(routes) == []


def test_every_operation_named_as_a_read_is_a_get(routes: list[Row]) -> None:
    assert _reads_not_sent_as_get(routes) == []


def test_the_checks_catch_a_snake_case_segment_a_trailing_slash_and_a_read_sent_as_a_post() -> None:
    good: Row = ("GET", "/api/tip-states/{device_id}", "get_tip_state")
    assert _paths_not_in_kebab_case([good]) == []
    assert _reads_not_sent_as_get([good, ("POST", "/api/modes", "set_device_mode")]) == []
    assert _paths_not_in_kebab_case([("GET", "/api/tip_states", "x")]) == ["/api/tip_states"]
    assert _paths_not_in_kebab_case([("GET", "/api/tips/", "x")]) == ["/api/tips/"]
    assert _reads_not_sent_as_get([("POST", "/api/tips", "get_tips")]) == ["POST /api/tips (get_tips)"]
