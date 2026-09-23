"""Tests for daemon/system_builder.py -- spec -> Topology / WorkflowTemplate.

The monolithic bundle loader is split into two resolvers:
``load_topology_spec`` (module:factory returning a Topology, called with a
store factory) and ``load_workflow_spec`` (module:factory returning a
WorkflowTemplate, called with the mounted topology). Each test targets one
error branch; the happy paths are covered by the route tests and the SMC
end-to-end run.
"""

import pytest

from orca.daemon.system_builder import (
    FactoryNotCallableError,
    FactoryNotFoundError,
    FactoryReturnShapeError,
    ModuleImportError,
    SpecFormatError,
    load_topology_spec,
    load_workflow_spec,
)
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import Topology


_TOPOLOGY_SPEC = "tests.daemon.daemon_test_fixture_topology:build_topology"


def _topology() -> Topology:
    return load_topology_spec(_TOPOLOGY_SPEC, InMemoryRuntimeStoreFactory())


# -- topology resolver -------------------------------------------------------


def test_topology_happy_path_returns_topology() -> None:
    topo = _topology()
    assert isinstance(topo, Topology)


def test_topology_spec_without_colon_raises_format_error() -> None:
    with pytest.raises(SpecFormatError, match="module:factory"):
        load_topology_spec("just.a.module.path", InMemoryRuntimeStoreFactory())


def test_topology_module_not_importable_raises() -> None:
    with pytest.raises(ModuleImportError):
        load_topology_spec(
            "orca.definitely_not_a_module_xyz:build_topology",
            InMemoryRuntimeStoreFactory(),
        )


def test_topology_factory_attr_missing_raises(tmp_path, monkeypatch) -> None:
    mod = tmp_path / "has_no_topology.py"
    mod.write_text("SOMETHING_ELSE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(FactoryNotFoundError, match="build_topology"):
        load_topology_spec(
            "has_no_topology:build_topology", InMemoryRuntimeStoreFactory(),
        )


def test_topology_factory_not_callable_raises(tmp_path, monkeypatch) -> None:
    mod = tmp_path / "non_callable_topology.py"
    mod.write_text("build_topology = 42\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(FactoryNotCallableError):
        load_topology_spec(
            "non_callable_topology:build_topology", InMemoryRuntimeStoreFactory(),
        )


def test_topology_wrong_return_shape_raises(tmp_path, monkeypatch) -> None:
    mod = tmp_path / "bad_topology_shape.py"
    mod.write_text("def build_topology(stores):\n    return 42\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(FactoryReturnShapeError, match="Topology"):
        load_topology_spec(
            "bad_topology_shape:build_topology", InMemoryRuntimeStoreFactory(),
        )


# -- workflow resolver -------------------------------------------------------


def test_workflow_happy_path_returns_template() -> None:
    topo = _topology()
    template = load_workflow_spec(
        "tests.daemon.daemon_test_fixture_topology:build_workflow", topo,
    )
    assert template.name == "simple_workflow"


def test_workflow_spec_without_colon_raises_format_error() -> None:
    with pytest.raises(SpecFormatError, match="module:factory"):
        load_workflow_spec("just.a.module.path", _topology())


def test_workflow_wrong_return_shape_raises(tmp_path, monkeypatch) -> None:
    mod = tmp_path / "bad_workflow_shape.py"
    mod.write_text("def build_workflow(topology):\n    return 42\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(FactoryReturnShapeError, match="WorkflowTemplate"):
        load_workflow_spec("bad_workflow_shape:build_workflow", _topology())
