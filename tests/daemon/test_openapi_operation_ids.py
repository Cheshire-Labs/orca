"""OpenAPI operationId + summary derivation for ``mount_api`` routes.

Without explicit derivation, FastAPI falls back to the inner handler
function's ``__name__`` (literally "handler") for both the operationId
and the summary of every binding. operationId drives generated-doc
filenames; summary drives page titles AND sidebar labels. Both must be
derived from the Operation or the REST docs render "Handler" everywhere.
"""

import pytest

from orca.daemon.app import create_app


@pytest.fixture
def schema() -> dict[str, object]:
    return create_app().openapi()


def _operation_ids_by_path(schema: dict[str, object]) -> dict[str, str]:
    """Collapse ``paths`` -> first-method-found operationId, per path.

    The specific cases below use paths that serve one method only.
    """
    out: dict[str, str] = {}
    paths = schema["paths"]
    assert isinstance(paths, dict)
    for path, methods in paths.items():
        assert isinstance(methods, dict)
        for method_payload in methods.values():
            assert isinstance(method_payload, dict)
            op_id = method_payload.get("operationId")
            if isinstance(op_id, str):
                out[path] = op_id
                break
    return out


def _summaries_by_path(schema: dict[str, object]) -> dict[str, str]:
    """Collapse ``paths`` -> first-method-found summary, per path."""
    out: dict[str, str] = {}
    paths = schema["paths"]
    assert isinstance(paths, dict)
    for path, methods in paths.items():
        assert isinstance(methods, dict)
        for method_payload in methods.values():
            assert isinstance(method_payload, dict)
            summary = method_payload.get("summary")
            if isinstance(summary, str):
                out[path] = summary
                break
    return out


def test_mount_api_routes_have_per_path_operation_ids(
    schema: dict[str, object],
) -> None:
    ids = _operation_ids_by_path(schema)
    bound_paths = {p for p in ids if p.startswith("/api/")}
    assert bound_paths, "expected /api routes to be registered"

    handler_paths = [p for p in bound_paths if ids[p].startswith("handler")]
    assert not handler_paths, (
        "mount_api routes fell back to operationId='handler' "
        f"(or handler_N): {handler_paths}"
    )


def test_specific_operation_ids_derived_from_the_operation(
    schema: dict[str, object],
) -> None:
    """Lock the derivation rule: the Operation class name in snake case."""
    ids = _operation_ids_by_path(schema)

    cases = {
        "/api/labware-instances/{labware_id}/barcode": "edit_labware_barcode",
        "/api/labware-instances/{labware_id}/discharge": "discharge_labware",
        "/api/workflows": "list_workflows",
        "/api/methods/{name}": "get_method",
        "/api/system": "get_system_info",
    }
    for path, expected in cases.items():
        assert path in ids, f"{path} not registered"
        assert ids[path] == expected, (
            f"{path} has operationId={ids[path]!r}, expected {expected!r}"
        )


def test_mount_api_routes_have_non_handler_summaries(
    schema: dict[str, object],
) -> None:
    summaries = _summaries_by_path(schema)
    bound_paths = {p for p in summaries if p.startswith("/api/")}
    assert bound_paths, "expected /api routes to be registered"

    handler_paths = [p for p in bound_paths if summaries[p] == "Handler"]
    assert not handler_paths, (
        "mount_api routes fell back to summary='Handler' "
        f"(renders as 'Handler' page titles + sidebar labels): {handler_paths}"
    )


def test_specific_summaries_derived_from_the_operation(
    schema: dict[str, object],
) -> None:
    """Lock the derivation rule: the Operation class name in words, title-cased."""
    summaries = _summaries_by_path(schema)

    cases = {
        "/api/labware-instances/{labware_id}/barcode": "Edit Labware Barcode",
        "/api/labware-instances/{labware_id}/discharge": "Discharge Labware",
        "/api/workflows": "List Workflows",
        "/api/methods/{name}": "Get Method",
        "/api/system": "Get System Info",
    }
    for path, expected in cases.items():
        assert path in summaries, f"{path} not registered"
        assert summaries[path] == expected, (
            f"{path} has summary={summaries[path]!r}, expected {expected!r}"
        )


def test_all_operation_ids_globally_unique(
    schema: dict[str, object],
) -> None:
    """operationId must be unique across the whole OpenAPI document.

    A collision would make downstream doc generators merge or
    overwrite pages, so guard the entire surface.
    """
    seen: dict[str, str] = {}
    collisions: list[str] = []
    paths = schema["paths"]
    assert isinstance(paths, dict)
    for path, methods in paths.items():
        assert isinstance(methods, dict)
        for method, payload in methods.items():
            assert isinstance(payload, dict)
            op_id = payload.get("operationId")
            if not isinstance(op_id, str):
                continue
            if op_id in seen:
                collisions.append(
                    f"{op_id!r}: {seen[op_id]} vs {method.upper()} {path}"
                )
            else:
                seen[op_id] = f"{method.upper()} {path}"
    assert not collisions, f"duplicate operationIds: {collisions}"
