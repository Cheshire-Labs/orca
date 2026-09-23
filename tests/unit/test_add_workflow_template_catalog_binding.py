"""``add_workflow_template`` must bind the system's labware catalog to
every labware template inside the registered workflow.

The static path (``build_system(labwares=[...])``) handles binding via
``SdkToSystemBuilder._derive_labwares`` + ``bind_catalog``. The dynamic
path (``add_workflow_template(system, workflow)`` called after the system
is already built, e.g. a hosted deployment's ``POST /api/workflows`` against an
already-running runtime) historically skipped the bind. Templates inside
the dynamically-added workflow then raised at execution time:

    LabwareTemplate 'X': no labware catalog is bound; SdkToSystemBuilder
    normally binds one when the System is constructed.

This test pins the contract: registering a workflow whose labware
templates are unbound must result in those templates being bound to the
system's catalog so ``create_instance()`` succeeds.
"""

from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.runtime.labware_catalog_protocol import ILabwareCatalog
from orca.sdk.build import add_workflow_template
from orca.variables.errors import VariableValidationError
from orca.variables.variable_definition import VariableDefinition
from orca.variables.variable_store import VariableService, VariableStore
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate


class _CatalogRequiringTemplate(LabwareTemplate):
    """LabwareTemplate that exercises ``_require_catalog()`` from
    ``create_instance()`` -- the same pattern real templates use
    (PlateTemplate, TipRackTemplate, etc.).
    """

    async def create_instance(self) -> LabwareInstance:
        self._require_catalog()
        return LabwareInstance(template_name=self._name, labware_type="test")


def _make_mock_system(catalog: ILabwareCatalog) -> MagicMock:
    """Build a duck-typed System exposing only the surface
    ``add_workflow_template`` consults.

    The function reads:
      * ``system.labware_catalog`` to source the catalog for binding
      * ``system.get_location(name)`` to resolve string locations
      * ``system.add_workflow_template`` to register the workflow
      * ``system.add_method_template(workflow_name, method)``
      * ``system.add_labware_thread_template(workflow_name, thread)``
    """
    system = MagicMock()
    system.labware_catalog = catalog
    system.get_location = lambda name: MagicMock(name=name)
    system.add_workflow_template = MagicMock()
    system.add_method_template = MagicMock()
    system.add_labware_thread_template = MagicMock()
    return system


async def _thread_func(ctx: object):  # pragma: no cover - signature only
    if False:
        yield


def _build_workflow_with_unbound_template(name: str, template: LabwareTemplate) -> WorkflowTemplate:
    """Build a workflow that owns a thread carrying an unbound template."""
    thread = ThreadTemplate(
        labware_template=template,
        start="start_loc",
        end="end_loc",
        func=_thread_func,
    )
    wf = WorkflowTemplate(name)
    wf.add_thread(thread, is_start=True)
    return wf


async def test_thread_template_labware_gets_catalog_bound() -> None:
    """After ``add_workflow_template``, the labware template carried by a
    ``thread_templates`` entry must have the system's catalog bound."""
    catalog = MagicMock(spec=ILabwareCatalog)
    template = _CatalogRequiringTemplate("dmso_reservoir")
    assert template._catalog is None, "precondition: template starts unbound"

    workflow = _build_workflow_with_unbound_template("hamilton_dmso", template)
    system = _make_mock_system(catalog)

    await add_workflow_template(system, workflow)

    assert template._catalog is catalog, (
        "add_workflow_template must bind the system catalog to every "
        "labware template inside the workflow's threads"
    )


async def test_bundled_thread_labware_gets_catalog_bound() -> None:
    """Bundled threads (declared inside @orca.workflow closure) must also
    have their labware templates bound to the catalog."""
    catalog = MagicMock(spec=ILabwareCatalog)
    template = _CatalogRequiringTemplate("bead_plate")
    assert template._catalog is None

    bundled_thread = ThreadTemplate(
        labware_template=template,
        start="loc_a",
        end="loc_b",
        func=_thread_func,
    )
    workflow = WorkflowTemplate("bundled_wf")
    workflow.attach_bundled_templates(methods=[], threads=[bundled_thread])
    system = _make_mock_system(catalog)

    await add_workflow_template(system, workflow)

    assert template._catalog is catalog, (
        "bundled_threads carry templates that also need catalog binding"
    )


async def test_create_instance_succeeds_after_add_workflow_template() -> None:
    """End-to-end: after registration, calling ``create_instance()`` on
    the template no longer raises the ``bind_catalog()`` precondition.

    This is the failure mode the AI authoring session hit: a workflow
    submitted dynamically failed at execution-time materialization with
    the bind_catalog precondition error.
    """
    catalog = MagicMock(spec=ILabwareCatalog)
    template = _CatalogRequiringTemplate("plate_1")
    workflow = _build_workflow_with_unbound_template("hamilton_smc", template)
    system = _make_mock_system(catalog)

    await add_workflow_template(system, workflow)

    # No raise here is the contract; calling create_instance() pre-fix
    # raised the bind_catalog() precondition error.
    instance = await template.create_instance()
    assert instance is not None


async def test_add_workflow_template_registers_variable_definitions() -> None:
    """Dynamic-path parity with the build path: a workflow added after build
    must register its variable definitions so defaults resolve and submitted
    values validate. Without this, ``ctx.param`` on a defaulted variable raised
    ``UndefinedVariableError`` for any workflow added via
    ``register_workflow_from_source`` (SdkToSystemBuilder registers definitions
    only for workflows present at build time).
    """
    catalog = MagicMock(spec=ILabwareCatalog)
    template = _CatalogRequiringTemplate("plate_1")
    workflow = _build_workflow_with_unbound_template("dynamic_wf", template)
    workflow.add_variable(
        "dilution_factor",
        VariableDefinition(type="float", default=10.0, min=2.0, max=100.0),
    )
    system = _make_mock_system(catalog)
    store = VariableService(VariableStore())
    system.variable_store = store

    await add_workflow_template(system, workflow)

    store.create_execution("exec-1", "dynamic_wf")
    assert store.resolve("dilution_factor", "exec-1") == 10.0
    # the registered definition also validates operator-supplied values
    with pytest.raises(VariableValidationError):
        store.set("dilution_factor", 999.0, "exec-1")
