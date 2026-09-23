"""Thread templates are namespaced by (workflow_name, thread_name).

Two workflows (the thread counterpart to the method rule) must be
able to declare a thread template with the same bare name (e.g.
``sample_plate`` in both ``hamilton_smc_assay_v1`` and ``_v2``) for A/B
testing and staged rollouts. Before this change the thread-template
registry was keyed deployment-wide by bare name, so registering the
second workflow's same-named thread raised
``ThreadTemplateNameCollisionError``.

Contract pinned here:
- Distinct thread objects with the same name in two workflows coexist.
- ``get_labware_thread_template(workflow, name)`` resolves within scope.
- A thread registered for workflow A is not visible under workflow B.
- Re-registering the SAME object under the SAME workflow is a no-op
  (identity check); a DIFFERENT object under the same (workflow, name)
  still collides -- the uniqueness rule is now per-workflow, not gone.
- Removing one workflow's thread does not clobber the other workflow's
  same-named thread (the cross-workflow cascade-clobber bug).
"""

from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.runtime.labware_catalog_protocol import ILabwareCatalog
from orca.sdk.build import add_workflow_template
from orca.system.registries import TemplateRegistry
from orca.workflow_models.thread_template import (
    ThreadTemplate,
    ThreadTemplateNameCollisionError,
)
from orca.workflow_models.workflow_templates import WorkflowTemplate


class _CatalogTemplate(LabwareTemplate):
    async def create_instance(self) -> LabwareInstance:
        return LabwareInstance(template_name=self._name, labware_type="test")


async def _thread_func(ctx: object):  # pragma: no cover - signature only
    if False:
        yield


def _make_thread(name: str) -> ThreadTemplate:
    return ThreadTemplate(
        labware_template=_CatalogTemplate(name),
        start="start_loc",
        end="end_loc",
        func=_thread_func,
    )


class TestRegistryQualifiedKey:
    def test_two_workflows_share_a_thread_name(self) -> None:
        reg = TemplateRegistry()
        t_v1 = _make_thread("sample_plate")
        t_v2 = _make_thread("sample_plate")

        reg.add_labware_thread_template("assay_v1", t_v1)
        reg.add_labware_thread_template("assay_v2", t_v2)

        assert reg.get_labware_thread_template("assay_v1", "sample_plate") is t_v1
        assert reg.get_labware_thread_template("assay_v2", "sample_plate") is t_v2

    def test_thread_not_visible_under_other_workflow(self) -> None:
        reg = TemplateRegistry()
        reg.add_labware_thread_template("assay_v1", _make_thread("tip_rack"))
        with pytest.raises(KeyError):
            reg.get_labware_thread_template("assay_v2", "tip_rack")

    def test_distinct_object_same_workflow_collides(self) -> None:
        reg = TemplateRegistry()
        t = _make_thread("tip_rack")
        reg.add_labware_thread_template("assay_v1", t)
        with pytest.raises(ThreadTemplateNameCollisionError):
            reg.add_labware_thread_template("assay_v1", _make_thread("tip_rack"))

    def test_same_object_same_workflow_is_idempotent(self) -> None:
        reg = TemplateRegistry()
        t = _make_thread("tip_rack")
        reg.add_labware_thread_template("assay_v1", t)
        reg.add_labware_thread_template("assay_v1", t)
        assert reg.get_labware_thread_template("assay_v1", "tip_rack") is t

    def test_get_thread_templates_keyed_by_pair(self) -> None:
        reg = TemplateRegistry()
        reg.add_labware_thread_template("assay_v1", _make_thread("sample_plate"))
        reg.add_labware_thread_template("assay_v2", _make_thread("sample_plate"))
        keys = set(reg.get_labware_thread_templates().keys())
        assert keys == {
            ("assay_v1", "sample_plate"),
            ("assay_v2", "sample_plate"),
        }

    def test_remove_is_scoped_to_workflow(self) -> None:
        reg = TemplateRegistry()
        t_v1 = _make_thread("sample_plate")
        t_v2 = _make_thread("sample_plate")
        reg.add_labware_thread_template("assay_v1", t_v1)
        reg.add_labware_thread_template("assay_v2", t_v2)

        removed = reg.remove_labware_thread_template("assay_v1", "sample_plate")
        assert removed is t_v1
        assert reg.get_labware_thread_template("assay_v2", "sample_plate") is t_v2


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


def _workflow_with_thread(workflow_name: str, thread: ThreadTemplate) -> WorkflowTemplate:
    wf = WorkflowTemplate(workflow_name)
    wf.add_thread(thread, is_start=True)
    wf.attach_bundled_templates(methods=[], threads=[thread])
    return wf


class TestAddWorkflowTemplateSharedThreadName:
    async def test_two_workflows_with_shared_thread_name_both_register(self) -> None:
        catalog = MagicMock(spec=ILabwareCatalog)
        system = _make_mock_system(catalog)

        wf_v1 = _workflow_with_thread("assay_v1", _make_thread("sample_plate"))
        wf_v2 = _workflow_with_thread("assay_v2", _make_thread("sample_plate"))

        await add_workflow_template(system, wf_v1)
        # Pre-fix this raised ThreadTemplateNameCollisionError.
        await add_workflow_template(system, wf_v2)

        reg = system._qualified_registry
        assert reg.get_labware_thread_template("assay_v1", "sample_plate") is not None
        assert reg.get_labware_thread_template("assay_v2", "sample_plate") is not None

    async def test_distinct_object_same_workflow_name_still_collides(self) -> None:
        catalog = MagicMock(spec=ILabwareCatalog)
        system = _make_mock_system(catalog)

        wf = _workflow_with_thread("assay_v1", _make_thread("sample_plate"))
        wf._bundled_threads.append(_make_thread("sample_plate"))

        with pytest.raises(ThreadTemplateNameCollisionError):
            await add_workflow_template(system, wf)
