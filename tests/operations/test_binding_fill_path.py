"""`RestBinding.fill_url` puts the path fields in the url and returns the rest."""

from orca.operations.binding import RestBinding


def _binding(path: str) -> RestBinding:
    return RestBinding(method="GET", path=path, tag="test")


def test_path_fields_fill_the_path_and_the_rest_come_back() -> None:
    path, rest = _binding("/executions/{execution_id}/threads").fill_url(
        {"execution_id": "exec-1", "limit": 5},
    )
    assert path == "/api/executions/exec-1/threads"
    assert rest == {"limit": 5}


def test_a_plain_path_field_is_percent_encoded_whole() -> None:
    path, _ = _binding("/variables/{execution_id}/{name}").fill_url(
        {"execution_id": "exec-1", "name": "a/b c"},
    )
    assert path == "/api/variables/exec-1/a%2Fb%20c"


def test_a_path_typed_field_keeps_its_slashes() -> None:
    path, rest = _binding("/teachpoints/{device_id}/{position_id:path}/labware/{labware_type}").fill_url(
        {"device_id": "dev-1", "position_id": "deck/slot 1", "labware_type": "costar_96", "reason": "r"},
    )
    assert path == "/api/teachpoints/dev-1/deck/slot%201/labware/costar_96"
    assert rest == {"reason": "r"}


def test_a_route_declared_outside_the_api_prefix_is_filled_at_its_bare_path() -> None:
    binding = RestBinding(method="GET", path="/things/{name}", tag="test", under_api=False)
    path, _ = binding.fill_url({"name": "a"})
    assert path == "/things/a"
