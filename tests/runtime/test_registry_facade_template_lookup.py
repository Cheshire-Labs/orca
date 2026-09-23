"""IRegistryFacade.get_method_template lookup by (workflow, name).

Wire callers (REST/MCP) accept a `template_name` arg for thread_insert_method
and need a server-side resolver. Method names are unique per workflow, so the
facade getter takes the workflow scope; the InsertMethod operation derives it
from the target thread's execution. The facade's existing
`list_method_templates` returns snapshots without callable templates; this
getter returns the runtime MethodTemplate so it can be passed to
`runtime.threads.insert_method`.

Action templates intentionally have no analogous getter: ActionTemplates
are not registered system-wide (they live inside @orca.method generator
bodies as @orca.action-decorated locals). Wire callers must inject
action source via the `action_code` path instead of name lookup.
"""

from unittest.mock import MagicMock

import pytest

from orca.runtime.facades.registry import RegistryFacade
from orca.runtime.registries.null_gateway_registry import NullDeviceConnectionSource


def _make_facade(method_templates: dict[tuple[str, str], object]) -> RegistryFacade:
    system = MagicMock()
    system.get_method_template = lambda workflow_name, name: method_templates[(workflow_name, name)]
    facade = RegistryFacade(
        system=system,
        list_reservations_fn=lambda eid: [],
        cancel_reservation_fn=lambda eid, rid: None,
        connections=NullDeviceConnectionSource(),
    )
    return facade


class TestGetMethodTemplate:
    def test_returns_template_by_workflow_and_name(self) -> None:
        sentinel = object()
        facade = _make_facade({("assay_v1", "incubate"): sentinel})

        result = facade.get_method_template("assay_v1", "incubate")

        assert result is sentinel

    def test_missing_template_raises_key_error(self) -> None:
        facade = _make_facade({})

        with pytest.raises(KeyError):
            facade.get_method_template("assay_v1", "missing")
