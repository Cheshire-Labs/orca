"""IRegistryFacade.add_workflow_template / remove_workflow_template.

These two methods are what a submission surface calls: a hosted service
calls add_workflow_template after writing a workflow file to git, and
calls remove_workflow_template via DELETE /api/workflows/{name}. The
@dangerous decoration means callers must pass confirm=True; remove
additionally requires reason=<str>.
"""

from unittest.mock import MagicMock

import pytest

from orca.runtime.danger import ConfirmationRequired
from orca.runtime.facades.registry import RegistryFacade
from orca.runtime.registries.null_gateway_registry import NullDeviceConnectionSource
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate


def _make_facade(workflow_templates: dict[str, WorkflowTemplate] | None = None) -> tuple[RegistryFacade, MagicMock]:
    """Build a RegistryFacade backed by a mock system whose registries
    are simple dicts. Returns the facade and the mock so tests can
    assert calls to the system mutators.
    """
    workflow_templates = workflow_templates or {}
    method_templates: dict[tuple[str, str], MethodTemplate] = {}
    thread_templates: dict[tuple[str, str], ThreadTemplate] = {}

    system = MagicMock()
    system.get_workflow_templates = lambda: workflow_templates
    system.get_method_templates = lambda: method_templates
    system.get_labware_thread_templates = lambda: thread_templates
    system.get_workflow_template = lambda name: workflow_templates[name]
    system.get_method_template = lambda workflow_name, name: method_templates[(workflow_name, name)]
    system.get_labware_thread_template = (
        lambda workflow_name, name: thread_templates[(workflow_name, name)]
    )
    system.get_location = lambda name: MagicMock(name=name)

    def _add_workflow(wf: WorkflowTemplate) -> None:
        if wf.name in workflow_templates:
            raise KeyError(f"Workflow {wf.name} already defined")
        workflow_templates[wf.name] = wf
    system.add_workflow_template = _add_workflow

    def _add_method(workflow_name: str, m: MethodTemplate) -> None:
        key = (workflow_name, m.name)
        if key in method_templates:
            raise KeyError(f"Method {m.name} already defined")
        method_templates[key] = m
    system.add_method_template = _add_method

    def _add_thread(workflow_name: str, t: ThreadTemplate) -> None:
        key = (workflow_name, t.name)
        if key in thread_templates:
            raise KeyError(f"Thread {t.name} already defined")
        thread_templates[key] = t
    system.add_labware_thread_template = _add_thread

    system.remove_workflow_template = lambda name: workflow_templates.pop(name, None)
    system.remove_method_template = (
        lambda workflow_name, name: method_templates.pop((workflow_name, name), None)
    )
    system.remove_labware_thread_template = (
        lambda workflow_name, name: thread_templates.pop((workflow_name, name), None)
    )

    facade = RegistryFacade(
        system=system,
        list_reservations_fn=lambda eid: [],
        cancel_reservation_fn=lambda eid, rid: None,
        connections=NullDeviceConnectionSource(),
    )
    return facade, system


def _make_workflow(name: str) -> WorkflowTemplate:
    """Build a minimal WorkflowTemplate with no threads or methods."""
    return WorkflowTemplate(name)


class TestAddWorkflowTemplate:
    @pytest.mark.asyncio
    async def test_add_without_confirm_raises_confirmation_required(self) -> None:
        facade, _ = _make_facade()
        wf = _make_workflow("walk8_wf")

        with pytest.raises(ConfirmationRequired):
            await facade.add_workflow_template(wf)

    @pytest.mark.asyncio
    async def test_add_with_confirm_registers_workflow(self) -> None:
        facade, system = _make_facade()
        wf = _make_workflow("walk8_wf")

        await facade.add_workflow_template(wf, confirm=True)

        assert "walk8_wf" in system.get_workflow_templates()

    @pytest.mark.asyncio
    async def test_add_with_existing_name_replaces(self) -> None:
        existing = _make_workflow("walk8_wf")
        facade, system = _make_facade({"walk8_wf": existing})
        new = _make_workflow("walk8_wf")

        await facade.add_workflow_template(new, confirm=True)

        assert system.get_workflow_templates()["walk8_wf"] is new


class TestRemoveWorkflowTemplate:
    @pytest.mark.asyncio
    async def test_remove_without_confirm_raises_confirmation_required(self) -> None:
        wf = _make_workflow("walk8_wf")
        facade, _ = _make_facade({"walk8_wf": wf})

        with pytest.raises(ConfirmationRequired):
            await facade.remove_workflow_template("walk8_wf")

    @pytest.mark.asyncio
    async def test_remove_without_reason_raises_value_error(self) -> None:
        wf = _make_workflow("walk8_wf")
        facade, _ = _make_facade({"walk8_wf": wf})

        with pytest.raises(ValueError, match="requires reason"):
            await facade.remove_workflow_template("walk8_wf", confirm=True)

    @pytest.mark.asyncio
    async def test_remove_with_confirm_and_reason_drops_workflow(self) -> None:
        wf = _make_workflow("walk8_wf")
        facade, system = _make_facade({"walk8_wf": wf})

        await facade.remove_workflow_template(
            "walk8_wf", confirm=True, reason="cleanup",
        )

        assert "walk8_wf" not in system.get_workflow_templates()

    @pytest.mark.asyncio
    async def test_remove_missing_raises_key_error(self) -> None:
        facade, _ = _make_facade()

        with pytest.raises(KeyError):
            await facade.remove_workflow_template(
                "missing", confirm=True, reason="cleanup",
            )


class TestGetWorkflowTemplate:
    def test_returns_template_by_name(self) -> None:
        wf = _make_workflow("walk8_wf")
        facade, _ = _make_facade({"walk8_wf": wf})

        assert facade.get_workflow_template("walk8_wf") is wf

    def test_missing_template_raises_key_error(self) -> None:
        facade, _ = _make_facade()

        with pytest.raises(KeyError):
            facade.get_workflow_template("missing")
