"""Compile @orca.method / @orca.action source from the wire into runtime templates.

Used by a hosted deployment's `thread_insert_method` / `thread_insert_action` REST/MCP
tools (and the orca-core daemon equivalents). Wire callers send the
Python source defining a single decorated function; this module compiles
it, executes it in an isolated namespace, and returns the resulting
`MethodTemplate` / `ActionTemplate` ready to hand to
`runtime.threads.insert_method` / `runtime.threads.insert_action`.

Injected code runs with the reach of the deployment's own code. This is a
single-tenant deployment: customers run on dedicated infrastructure with
their own data, and the source arrives over an authenticated wire. The
guards below are NOT a sandbox and cannot be made into one. They catch the
accident an AI client actually makes -- reaching for `os.system(...)` or
`open("/etc/passwd")` when it meant to drive a device -- and they do it at
the point of use, so an indirect reach fails the same way a direct one does.

Imports the source can use:
- orca.* (decorators + runtime types)
- cheshire_drivers.* (device interfaces + labware)
- deployment_package.* (the customer's deployed code; references to
  topology devices and labware templates flow through here at runtime)
- typing, asyncio, dataclasses (commonly used in user code)
"""

import builtins
import sys
from collections.abc import Callable
from types import CodeType, ModuleType
from typing import Any, NoReturn, TypeVar

from orca.resource_models.resource_pool import ResourcePool
from orca.system.system_interface import ISystem
from orca.workflow_models.action_template import Action
from orca.workflow_models.method_template import (
    MethodTemplate,
    _PENDING_METHOD_TEMPLATES,
)

_DeviceT = TypeVar("_DeviceT")


class LiveTopology:
    """Live device/pool resolver injected into ad-hoc insert_* code as
    ``topology``. Mirrors ``sdk.build.Topology.device`` / ``.pool`` but
    reads the RUNNING system, so an injected
    ``@orca.action(device=topology.device(name, T))`` binds the exact same
    Device object the reservation system resolves against. Grants the same
    device-resolution reach a workflow file's ``build_workflow(topology)``
    already has, no more (single-tenant; see module docstring).
    """

    def __init__(self, system: ISystem) -> None:
        self._system = system

    def device(self, name: str, expected_type: type[_DeviceT]) -> _DeviceT:
        try:
            resource = self._system.get_device(name)
        except KeyError:
            available = ", ".join(sorted(d.name for d in self._system.devices))
            raise KeyError(
                f"No device named {name!r}. Available: {available}"
            ) from None
        if not isinstance(resource, expected_type):
            raise TypeError(
                f"Device {name!r} is {type(resource).__name__}, "
                f"expected {expected_type.__name__}"
            )
        return resource

    def pool(self, name: str) -> ResourcePool:
        try:
            return self._system.get_resource_pool(name)
        except KeyError:
            available = ", ".join(sorted(p.name for p in self._system.resource_pools))
            raise KeyError(
                f"No pool named {name!r}. Available: {available}"
            ) from None


_ALLOWED_IMPORT_PREFIXES: tuple[str, ...] = (
    "orca",
    "cheshire_drivers",
    "deployment_package",
    "typing",
    "asyncio",
    "dataclasses",
)


# Each is replaced by a stub that raises, so an indirect route to one
# (`f = open` then `f(...)`) fails at the call exactly like a direct one.
_BLOCKED_BUILTINS: frozenset[str] = frozenset({
    "eval",
    "exec",
    "compile",
    "open",
    "breakpoint",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
    "input",
})


class CodeValidationError(ValueError):
    """Source was rejected: it did not compile, or it reached for something
    the injection namespace refuses to hand over.

    Carries `line` / `column` / `code` / `message` so wire layers can
    map cleanly to error envelopes (`code_validation_error`).

    ``column`` is ``None`` for a rejection raised while the source was
    running, where the traceback locates the line but not the token.
    ``line`` is ``None`` only when neither the compiler nor the traceback
    could report a position.
    """

    def __init__(self, line: int | None, column: int | None, code: str, message: str) -> None:
        if line is not None and column is not None:
            super().__init__(f"L{line}C{column} {code}: {message}")
        else:
            super().__init__(f"{code}: {message}")
        self.line = line
        self.column = column
        self.code = code
        self.message = message


class CodeInjectionError(RuntimeError):
    """Source executed but produced no usable template, raised at exec, or
    declared the wrong number of decorated entries.

    Distinct from `CodeValidationError` so wire layers can distinguish
    "rejected before run" from "ran but bad output".
    """


def compile_method_code(
    code: str, topology: LiveTopology | None = None,
) -> MethodTemplate:
    """Compile + execute injected Python defining a single @orca.method.

    The source must contain exactly one top-level @orca.method
    decoration. Helper imports and other top-level definitions are
    allowed but the wire contract is "one method per injection".

    `topology` pre-binds a live device resolver so injected method bodies
    can declare device-targeting actions exactly as a workflow file does
    (`topology.device("mlstar_1", LiquidHandler)`); `orca` is always
    pre-bound. Returns the resulting MethodTemplate, ready to hand to
    `runtime.threads.insert_method`.

    Raises:
        CodeValidationError: the source did not compile, or reached for a
            blocked builtin or an import outside the allow-list.
        CodeInjectionError: exec raised, or the source did not produce
            exactly one MethodTemplate.
    """
    compiled = _compile(code, kind="method")

    # Snapshot the pending-method queue so we can detect what THIS exec
    # appends (the @orca.method decorator's side effect) and rewind it
    # afterwards -- runtime injection must never leak into the
    # build-time catalog queue that SdkToSystemBuilder drains.
    pre_pending = list(_PENDING_METHOD_TEMPLATES)

    namespace = _injection_namespace(topology)
    try:
        exec(compiled, namespace)
    except CodeValidationError:
        _PENDING_METHOD_TEMPLATES[:] = pre_pending
        raise
    except Exception as exc:
        _PENDING_METHOD_TEMPLATES[:] = pre_pending
        raise CodeInjectionError(
            f"injected method source raised during execution: {exc!r}"
        ) from exc

    added = _PENDING_METHOD_TEMPLATES[len(pre_pending):]
    _PENDING_METHOD_TEMPLATES[:] = pre_pending  # always rewind

    if len(added) == 0:
        raise CodeInjectionError(
            "injected source did not define any @orca.method "
            "(must contain exactly one decorated method)"
        )
    if len(added) > 1:
        names = [t.name for t in added]
        raise CodeInjectionError(
            f"injected source defined multiple @orca.method "
            f"({names!r}); must contain exactly one"
        )
    template = added[0]
    template.injected_source = code
    return template


def compile_action_code(
    code: str, topology: LiveTopology | None = None,
) -> Action:
    """Compile + execute injected Python defining a single @orca.action.

    The source must contain exactly one top-level @orca.action
    decoration. Helper imports and untyped helpers are allowed.

    `topology` pre-binds a live device resolver so injected actions can
    target devices exactly as a workflow file does
    (`@orca.action(device=topology.device("delidder", Delidder), ...)`);
    `orca` is always pre-bound. Returns the resulting Action (an
    ActionTemplate subclass), ready to hand to
    `runtime.threads.insert_action`. The narrower return type matches the
    namespace walk's `isinstance(v, Action)` filter -- callers that only
    need ActionTemplate methods still work via inheritance.

    Raises:
        CodeValidationError: the source did not compile, or reached for a
            blocked builtin or an import outside the allow-list.
        CodeInjectionError: exec raised or wrong number of actions.
    """
    compiled = _compile(code, kind="action")

    namespace = _injection_namespace(topology)
    try:
        exec(compiled, namespace)
    except CodeValidationError:
        raise
    except Exception as exc:
        raise CodeInjectionError(
            f"injected action source raised during execution: {exc!r}"
        ) from exc

    actions = [
        v for k, v in namespace.items()
        if not k.startswith("_") and isinstance(v, Action)
    ]
    if len(actions) == 0:
        raise CodeInjectionError(
            "injected source did not define any @orca.action "
            "(must contain exactly one decorated action)"
        )
    if len(actions) > 1:
        names = [a.name for a in actions]
        raise CodeInjectionError(
            f"injected source defined multiple @orca.action "
            f"({names!r}); must contain exactly one"
        )
    action = actions[0]
    action.injected_source = code
    return action


def _compile(source: str, kind: str) -> CodeType:
    """Compile `source`, turning a SyntaxError into a positioned rejection."""
    filename = f"<injected_{kind}>"
    try:
        return compile(source, filename, "exec")
    except SyntaxError as exc:
        raise CodeValidationError(
            line=exc.lineno,
            column=exc.offset,
            code="syntax_error",
            message=exc.msg or "invalid syntax",
        ) from exc


def _injection_namespace(topology: LiveTopology | None) -> dict[str, Any]:
    """Namespace for an injected action/method exec.

    Pre-binds ``orca`` (so ``@orca.action`` / ``@orca.method`` resolve
    without the snippet importing it) and, when available, ``topology`` (the
    live resolver) so injected code references devices exactly as a workflow
    file does: ``topology.device("mlstar_1", LiquidHandler)``. Imported
    lazily to avoid an import cycle through the orca SDK package.
    """
    import orca.orca as orca_sdk

    namespace: dict[str, Any] = {
        "orca": orca_sdk,
        "__builtins__": _injection_builtins(),
    }
    if topology is not None:
        namespace["topology"] = topology
    return namespace


def _injection_builtins() -> dict[str, Any]:
    """The builtins an injected snippet sees: blocked names raise, imports
    are checked against the allow-list as they happen."""
    namespace = dict(vars(builtins))
    for name in _BLOCKED_BUILTINS:
        namespace[name] = _blocked_builtin(name)
    namespace["__import__"] = _guarded_import
    return namespace


def _blocked_builtin(name: str) -> Callable[..., NoReturn]:
    def blocked(*args: Any, **kwargs: Any) -> NoReturn:
        raise CodeValidationError(
            line=_offending_line(),
            column=None,
            code="forbidden_builtin",
            message=f"call to {name!r} is blocked",
        )

    return blocked


def _guarded_import(
    name: str,
    globals_: dict[str, Any] | None = None,
    locals_: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> ModuleType:
    if level != 0:
        raise CodeValidationError(
            line=_offending_line(),
            column=None,
            code="forbidden_import",
            message="relative imports are not allowed in injected source",
        )
    if not _is_allowed_module(name):
        raise CodeValidationError(
            line=_offending_line(),
            column=None,
            code="forbidden_import",
            message=f"import {name!r} is not in the allow-list",
        )
    return builtins.__import__(name, globals_, locals_, fromlist, level)


def _offending_line() -> int | None:
    """The injected source's line that reached the guard, from the live stack.

    Nothing has been raised yet, so there is no traceback to read: the
    innermost caller frame compiled from injected source is the location.
    Returns None if the guard was reached from somewhere else.
    """
    caller = sys._getframe(1)
    while caller is not None:
        if caller.f_code.co_filename.startswith("<injected_"):
            return caller.f_lineno
        caller = caller.f_back
    return None


def _is_allowed_module(name: str) -> bool:
    for prefix in _ALLOWED_IMPORT_PREFIXES:
        if name == prefix or name.startswith(prefix + "."):
            return True
    return False
