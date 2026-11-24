"""The verb list comes from the live Typer app and the route list from the app's OpenAPI document."""

import typer.main
from typer._click.core import Command
from typer.core import TyperGroup

from orca.cli.app import app as cli_app
from orca.daemon.app import create_app
from tests.surface.cli_cases import CLI_CASES, CLI_REFUSAL_ONLY, CliCase
from tests.surface.lifecycle_cases import LIFECYCLE_CLI_CASES, LIFECYCLE_REST_CASES
from tests.surface.rest_cases import ALL_CASES, REFUSAL_ONLY, RestCase

_HTTP = {"get", "post", "put", "patch", "delete"}


def live_verbs(group: Command, prefix: tuple[str, ...] = ()) -> list[str]:
    if not isinstance(group, TyperGroup):
        return [" ".join(prefix)]
    verbs: list[str] = []
    for name, command in sorted(group.commands.items()):
        verbs += live_verbs(command, (*prefix, name))
    return verbs


def live_routes() -> list[str]:
    paths = create_app().openapi()["paths"]
    return sorted(f"{method.upper()} {path}" for path, ops in paths.items() for method in ops if method in _HTTP)


def uncovered(live: list[str], covered: set[str]) -> list[str]:
    return [item for item in live if item not in covered]


def refusal_only(cases: list[RestCase] | list[CliCase]) -> set[str]:
    ok = {c.key if isinstance(c, RestCase) else c.verb for c in cases if _succeeds(c)}
    every = {c.key if isinstance(c, RestCase) else c.verb for c in cases}
    return every - ok


def _succeeds(case: RestCase | CliCase) -> bool:
    return (case.status < 400) if isinstance(case, RestCase) else case.exit_code == 0


def test_every_cli_verb_has_a_case() -> None:
    verbs = live_verbs(typer.main.get_command(cli_app))
    missing = uncovered(verbs, {c.verb for c in [*CLI_CASES, *LIFECYCLE_CLI_CASES]})
    assert not missing, f"verbs with no case: {missing}"


def test_every_rest_route_has_a_case() -> None:
    missing = uncovered(live_routes(), {c.key for c in [*ALL_CASES, *LIFECYCLE_REST_CASES]})
    assert not missing, f"routes with no case: {missing}"


def test_no_case_names_a_verb_or_route_that_does_not_exist() -> None:
    verbs = set(live_verbs(typer.main.get_command(cli_app)))
    routes = set(live_routes())
    assert not {c.verb for c in [*CLI_CASES, *LIFECYCLE_CLI_CASES]} - verbs
    assert not {c.key for c in [*ALL_CASES, *LIFECYCLE_REST_CASES]} - routes


def test_the_refusal_only_tables_list_exactly_the_cases_that_never_succeed() -> None:
    assert refusal_only([*ALL_CASES, *LIFECYCLE_REST_CASES]) == set(REFUSAL_ONLY)
    assert refusal_only([*CLI_CASES, *LIFECYCLE_CLI_CASES]) == set(CLI_REFUSAL_ONLY)


def test_the_guard_names_a_verb_that_has_no_case() -> None:
    assert uncovered(["a b", "c"], {"c"}) == ["a b"]


def test_the_guard_finds_nothing_when_every_item_has_a_case() -> None:
    assert uncovered(["a b", "c"], {"a b", "c", "extra"}) == []
