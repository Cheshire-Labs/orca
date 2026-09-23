"""OpenAPI operationId + summary derivation for ``bind_orca_rest`` routes.

Without explicit derivation, FastAPI falls back to the inner handler
function's ``__name__`` (literally "handler") for both the operationId
and the summary of every binding. operationId drives generated-doc
filenames; summary drives page titles AND sidebar labels. Both must be
derived from the path or the REST docs render "Handler" everywhere.
"""

import pytest

from orca.daemon.app import create_app


@pytest.fixture
def schema() -> dict[str, object]:
    return create_app(initial_system_runtime=None).openapi()


def _operation_ids_by_path(schema: dict[str, object]) -> dict[str, str]:
    """Collapse ``paths`` -> first-method-found operationId, per path.

    Every ``bind_orca_rest`` call wires exactly one method per path, so
    the first-method-found shortcut is unambiguous for those paths.
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


def test_bind_orca_rest_routes_have_per_path_operation_ids(
    schema: dict[str, object],
) -> None:
    ids = _operation_ids_by_path(schema)
    bound_paths = {p for p in ids if p.startswith("/operations/")}
    assert bound_paths, "expected /operations/* routes to be registered"

    handler_paths = [p for p in bound_paths if ids[p].startswith("handler")]
    assert not handler_paths, (
        "bind_orca_rest routes fell back to operationId='handler' "
        f"(or handler_N): {handler_paths}"
    )


def test_specific_operation_ids_derived_from_path(
    schema: dict[str, object],
) -> None:
    """Lock the derivation rule (last path segment, dashes -> underscores)."""
    ids = _operation_ids_by_path(schema)

    cases = {
        "/operations/edit-labware-barcode": "edit_labware_barcode",
        "/operations/submit-execution": "submit_execution",
        "/operations/list-executions": "list_executions",
        "/operations/get-method": "get_method",
        "/operations/system-info": "system_info",
    }
    for path, expected in cases.items():
        assert path in ids, f"{path} not registered"
        assert ids[path] == expected, (
            f"{path} has operationId={ids[path]!r}, expected {expected!r}"
        )


def test_bind_orca_rest_routes_have_non_handler_summaries(
    schema: dict[str, object],
) -> None:
    summaries = _summaries_by_path(schema)
    bound_paths = {p for p in summaries if p.startswith("/operations/")}
    assert bound_paths, "expected /operations/* routes to be registered"

    handler_paths = [p for p in bound_paths if summaries[p] == "Handler"]
    assert not handler_paths, (
        "bind_orca_rest routes fell back to summary='Handler' "
        f"(renders as 'Handler' page titles + sidebar labels): {handler_paths}"
    )


def test_specific_summaries_derived_from_path(
    schema: dict[str, object],
) -> None:
    """Lock the derivation rule (last path segment, dashes -> spaces, title-cased)."""
    summaries = _summaries_by_path(schema)

    cases = {
        "/operations/edit-labware-barcode": "Edit Labware Barcode",
        "/operations/submit-execution": "Submit Execution",
        "/operations/list-executions": "List Executions",
        "/operations/get-method": "Get Method",
        "/operations/system-info": "System Info",
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

    Bound ``/operations/*`` routes get an explicit per-path
    operation_id (bare last segment); direct routes keep FastAPI's
    default ``{name}_{path}_{method}`` form, so the two families do not
    overlap. A collision would make downstream doc generators merge or
    overwrite pages, so guard the entire surface -- not just
    ``/operations/*``.
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
