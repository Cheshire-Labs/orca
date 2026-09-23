"""Resolve a user's topology / workflow factory specs for the daemon.

`spec` is `module:factory` -- e.g. `examples.hamilton_smc.topology:build_topology`.

The daemon ingress is split:
- `load_topology_spec(spec, stores)` calls a `build_topology(stores)` factory
  and returns the `Topology` the daemon builds an empty SystemRuntime from.
- `load_workflow_spec(spec, topology)` calls a `build_workflow(topology)`
  factory and returns the `WorkflowTemplate` to register on the running
  runtime.

Errors are raised as typed exceptions so the route layer can map them to
HTTP status codes:
- SpecFormatError            -> 400 (malformed spec; missing ":")
- ModuleImportError          -> 404 (module not on sys.path)
- FactoryNotFoundError       -> 404 (attribute doesn't exist on module)
- FactoryNotCallableError    -> 400 (attribute exists but isn't callable)
- FactoryReturnShapeError    -> 400 (factory returned the wrong type)
"""

import importlib
from pathlib import Path
from typing import Callable, TypeVar

from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology
from orca.workflow_models.workflow_templates import WorkflowTemplate


class SpecFormatError(ValueError):
    """`spec` was not in `module:factory` form."""


class ModuleImportError(LookupError):
    """`module` part of the spec could not be imported."""


class FactoryNotFoundError(LookupError):
    """`factory` attribute does not exist on the imported module."""


class FactoryNotCallableError(TypeError):
    """`factory` attribute exists but isn't callable."""


class FactoryReturnShapeError(TypeError):
    """Factory returned an object of the wrong type."""


_T = TypeVar("_T")


def _resolve_factory(spec: str) -> Callable[..., object]:
    """Import and return the callable named by a `module:factory` spec."""
    if ":" not in spec:
        raise SpecFormatError(
            f"spec must be 'module:factory', got {spec!r}",
        )
    module_name, factory_name = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        raise ModuleImportError(
            f"cannot import {module_name!r}: {e}. The daemon imports specs from "
            f"the directory it was started in ({Path.cwd()}) and from installed packages.",
        ) from e

    factory = getattr(module, factory_name, None)
    if factory is None:
        raise FactoryNotFoundError(
            f"module {module_name!r} has no attribute {factory_name!r}",
        )
    if not callable(factory):
        raise FactoryNotCallableError(
            f"{spec!r} is not callable",
        )
    return factory


def load_topology_spec(spec: str, stores: IRuntimeStoreFactory) -> Topology:
    """Resolve a `module:build_topology` spec to a `Topology`.

    The factory is called with the daemon's store factory so calibration
    registries (teachpoints, deck layouts) seed correctly.
    """
    factory = _resolve_factory(spec)
    result = factory(stores)
    if not isinstance(result, Topology):
        raise FactoryReturnShapeError(
            f"{spec!r} returned {type(result).__name__!r}; expected "
            "orca.sdk.build.Topology",
        )
    return result


def load_workflow_spec(spec: str, topology: Topology) -> WorkflowTemplate:
    """Resolve a `module:build_workflow` spec to a `WorkflowTemplate`.

    The factory is called with the already-mounted topology so device
    references resolve against the live system map.
    """
    factory = _resolve_factory(spec)
    result = factory(topology)
    if not isinstance(result, WorkflowTemplate):
        raise FactoryReturnShapeError(
            f"{spec!r} returned {type(result).__name__!r}; expected "
            "WorkflowTemplate",
        )
    return result
