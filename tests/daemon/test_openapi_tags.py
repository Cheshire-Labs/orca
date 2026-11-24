"""OpenAPI tag coverage for every daemon REST route.

Without a tag on each route, ``docusaurus-plugin-openapi-docs`` (and
similar OpenAPI viewers) bucket every endpoint under a single
"UNTAGGED" category, which makes the rendered REST API impossible to
navigate.
"""

import pytest

from orca.daemon.app import create_app
from orca.operations.route_tags import RouteTag


@pytest.fixture
def schema() -> dict[str, object]:
    return create_app().openapi()


def _tags_by_path(schema: dict[str, object]) -> dict[str, list[str]]:
    """Collapse ``paths`` -> first-method-found tags, per path.

    Each route registration declares one method per path in this
    daemon, so the first-method-found shortcut is unambiguous.
    """
    out: dict[str, list[str]] = {}
    paths = schema["paths"]
    assert isinstance(paths, dict)
    for path, methods in paths.items():
        assert isinstance(methods, dict)
        for method_payload in methods.values():
            assert isinstance(method_payload, dict)
            tags = method_payload.get("tags") or []
            assert isinstance(tags, list)
            out[path] = [t for t in tags if isinstance(t, str)]
            break
    return out


def test_every_route_has_at_least_one_tag(schema: dict[str, object]) -> None:
    """No UNTAGGED bucket in the generated docs sidebar."""
    tags = _tags_by_path(schema)
    untagged = [p for p, ts in tags.items() if not ts]
    assert not untagged, f"routes missing OpenAPI tags: {untagged}"


def test_specific_tag_assignments(schema: dict[str, object]) -> None:
    """Spot-check the path -> tag grouping the issue calls for.

    Keeps the test honest if someone later moves a route under the
    wrong section by accident.
    """
    tags = _tags_by_path(schema)
    cases = {
        "/health": "lifecycle",
        "/api/runtime/mount-package": "lifecycle",
        "/api/runtime/unload": "lifecycle",
        "/api/executions": "executions",
        "/api/executions/{execution_id}/threads": "threads",
        "/api/executions/{execution_id}/reservations/{reservation_id}": "reservations",
        "/api/reservations": "reservations",
        "/api/variables": "variables",
        "/api/incidents": "incidents",
        "/api/teachpoints": "teachpoints",
        "/api/deck-layouts": "deck-layouts",
        "/api/access-configs": "access-configs",
        "/api/devices/{device_id}/introspection": "devices",
        "/api/plugins": "plugins",
        "/api/audit": "audit",
        "/api/events": "events",
        "/api/submissions": "submissions",
        "/api/submissions/{submission_id}": "submissions",
        "/api/system": "system",
        "/api/pause": "threads",
        "/api/resume": "threads",
        "/api/executions/{execution_id}/threads/{thread_id}/skip-method": "threads",
        "/api/executions/{execution_id}/close": "executions",
        "/api/executions/{execution_id}/stop": "executions",
        "/api/executions/{execution_id}/sim-speedup": "executions",
        "/api/labware-instances": "labware",
        "/api/labware-instances/{labware_id}/discharge": "labware",
        "/api/labware-instances/{labware_id}/barcode": "labware",
        "/api/devices/{device_id}/initialize": "devices",
        "/api/workflows": "catalog",
        "/api/methods/{name}": "catalog",
    }
    for path, expected in cases.items():
        assert path in tags, f"{path} not registered"
        assert expected in tags[path], (
            f"{path} tagged {tags[path]!r}, expected {expected!r}"
        )


def test_tags_are_kebab_case_strings(schema: dict[str, object]) -> None:
    """Each tag is a kebab-case string.

    Spaces and uppercase break sidebar grouping on the website's
    docusaurus pipeline.
    """
    tags = _tags_by_path(schema)
    bad: dict[str, list[str]] = {}
    for path, route_tags in tags.items():
        bogus = [t for t in route_tags if t != t.lower() or " " in t or "_" in t]
        if bogus:
            bad[path] = bogus
    assert not bad, f"non-kebab-case tags: {bad}"


def test_every_tag_is_a_known_route_tag(schema: dict[str, object]) -> None:
    """Tags come from the RouteTag enum, not ad-hoc string literals."""
    known = {t.value for t in RouteTag}
    tags = _tags_by_path(schema)
    used = {t for route_tags in tags.values() for t in route_tags}
    unknown = used - known
    assert not unknown, f"tags outside the RouteTag vocabulary: {unknown}"
