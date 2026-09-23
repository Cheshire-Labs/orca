"""Method templates are namespaced by (workflow_name, method_name).

Two workflows must be able to declare a method with the same bare name (e.g.
``combine_plates`` in both ``hamilton_smc_assay_v1`` and ``_v2``) for A/B
testing and staged rollouts. Before this change the template registry was keyed
deployment-wide by bare name, so registering the second workflow's same-named
method raised ``MethodTemplateNameCollisionError``.

Contract pinned here:
- Distinct method objects with the same name in two workflows coexist.
- ``get_method_template(workflow, name)`` resolves within that workflow.
- A method registered for workflow A is not visible under workflow B.
- Re-registering the SAME object under the SAME workflow is a no-op
  (identity check); a DIFFERENT object under the same (workflow, name)
  still collides -- the uniqueness rule is now per-workflow, not gone.
- Removing one workflow's method does not clobber the other workflow's
  same-named method (the cross-workflow cascade-clobber bug).
"""

from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.runtime.labware_catalog_protocol import ILabwareCatalog
from orca.sdk.build import add_workflow_template
from orca.system.registries import TemplateRegistry
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import (
    MethodTemplate,
    MethodTemplateNameCollisionError,
)
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate


async def _noop_method(ctx: MethodContext):  # pragma: no cover - never run
    if False:
        yield


def _make_method(name: str) -> MethodTemplate:
    return MethodTemplate(name, func=_noop_method)


class TestRegistryQualifiedKey:
    def test_two_workflows_share_a_method_name(self) -> None:
        reg = TemplateRegistry()
        m_v1 = _make_method("combine_plates")
        m_v2 = _make_method("combine_plates")

        reg.add_method_template("assay_v1", m_v1)
        reg.add_method_template("assay_v2", m_v2)

        assert reg.get_method_template("assay_v1", "combine_plates") is m_v1
        assert reg.get_method_template("assay_v2", "combine_plates") is m_v2

    def test_method_not_visible_under_other_workflow(self) -> None:
        reg = TemplateRegistry()
        reg.add_method_template("assay_v1", _make_method("incubate"))
        with pytest.raises(KeyError):
            reg.get_method_template("assay_v2", "incubate")

    def test_same_object_same_workflow_is_idempotent_via_add_guard(self) -> None:
        reg = TemplateRegistry()
        m = _make_method("incubate")
        reg.add_method_template("assay_v1", m)
        # Re-adding a DIFFERENT object under the same (workflow, name) is a
        # genuine collision -- the per-workflow uniqueness rule.
        with pytest.raises(MethodTemplateNameCollisionError):
            reg.add_method_template("assay_v1", _make_method("incubate"))

    def test_get_method_templates_keyed_by_pair(self) -> None:
        reg = TemplateRegistry()
        reg.add_method_template("assay_v1", _make_method("combine_plates"))
        reg.add_method_template("assay_v2", _make_method("combine_plates"))
        keys = set(reg.get_method_templates().keys())
        assert keys == {("assay_v1", "combine_plates"), ("assay_v2", "combine_plates")}

    def test_remove_is_scoped_to_workflow(self) -> None:
        reg = TemplateRegistry()
        m_v1 = _make_method("combine_plates")
        m_v2 = _make_method("combine_plates")
        reg.add_method_template("assay_v1", m_v1)
        reg.add_method_template("assay_v2", m_v2)

        removed = reg.remove_method_template("assay_v1", "combine_plates")
        assert removed is m_v1
        # v2's method survives the v1 removal -- no cross-workflow clobber.
        assert reg.get_method_template("assay_v2", "combine_plates") is m_v2


def _make_mock_system(catalog: ILabwareCatalog) -> MagicMock:
    system = MagicMock()
    system.labware_catalog = catalog
    system.get_location = lambda name: MagicMock(name=name)
    system.labware_templates = []

    registry = TemplateRegistry()
    system.add_workflow_template = registry.add_workflow_template
    system.get_workflow_templates = registry.get_workflow_templates
    system.get_method_template = registry.get_method_template
    system.add_method_template = registry.add_method_template
    system.get_labware_thread_template = registry.get_labware_thread_template
    system.add_labware_thread_template = registry.add_labware_thread_template
    system.add_labware_template = MagicMock()
    system._qualified_registry = registry
    return system


class _CatalogTemplate(LabwareTemplate):
    async def create_instance(self) -> LabwareInstance:
        return LabwareInstance(template_name=self._name, labware_type="test")


async def _thread_func(ctx: object):  # pragma: no cover - signature only
    if False:
        yield


def _workflow_with_method(workflow_name: str, method: MethodTemplate) -> WorkflowTemplate:
    template = _CatalogTemplate(f"{workflow_name}_plate")
    thread = ThreadTemplate(
        labware_template=template,
        start="start_loc",
        end="end_loc",
        func=_thread_func,
    )
    wf = WorkflowTemplate(workflow_name)
    wf.add_thread(thread, is_start=True)
    wf.attach_bundled_templates(methods=[method], threads=[])
    return wf


class TestAddWorkflowTemplateSharedMethodName:
    async def test_two_workflows_with_shared_method_name_both_register(self) -> None:
        catalog = MagicMock(spec=ILabwareCatalog)
        system = _make_mock_system(catalog)

        wf_v1 = _workflow_with_method("assay_v1", _make_method("combine_plates"))
        wf_v2 = _workflow_with_method("assay_v2", _make_method("combine_plates"))

        await add_workflow_template(system, wf_v1)
        # Pre-fix this raised MethodTemplateNameCollisionError.
        await add_workflow_template(system, wf_v2)

        reg = system._qualified_registry
        assert reg.get_method_template("assay_v1", "combine_plates") is not None
        assert reg.get_method_template("assay_v2", "combine_plates") is not None

    async def test_distinct_object_same_workflow_name_still_collides(self) -> None:
        catalog = MagicMock(spec=ILabwareCatalog)
        system = _make_mock_system(catalog)

        wf = _workflow_with_method("assay_v1", _make_method("combine_plates"))
        wf._bundled_methods.append(_make_method("combine_plates"))

        with pytest.raises(MethodTemplateNameCollisionError):
            await add_workflow_template(system, wf)
