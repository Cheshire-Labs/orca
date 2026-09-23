"""Uniform safety gating for mutation operations across every UI.

Every runtime mutation that can harm state (hardware, in-flight executions,
calibration history, etc.) is decorated with `@dangerous`. The decorator:

1. Records the method in a module-level `ActionRegistry` so UIs can list and
   describe every dangerous action (`list_actions`, `describe_action`).
2. Enforces at call time that the caller pass `confirm=True`. If False, raises
   `ConfirmationRequired` carrying the `ActionDescriptor` so the UI can render
   the right prompt without hardcoding message copy.

UIs consume the registry uniformly:

  - CLI: reads `describe_action(name)` to pick y/N vs typed-phrase prompt,
    then re-calls the method with `confirm=True` after operator approves.
  - REST (future): rejects calls without `confirm: true` in the body, returns
    the descriptor so the client can show a confirmation screen.
  - MCP (future): tool schemas are auto-generated from the registry so the LLM
    sees each tool's danger level and must ask the user before setting
    `confirm: True`.

The action registry is populated at import time: decorating a method calls
`register_action(...)`. Downstream lookup is by action name (e.g.
`"labware.edit_location"`).
"""

import dataclasses
import functools
import inspect
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable

from pydantic import BaseModel, JsonValue

audit_logger = logging.getLogger("orca.audit")

# Strip CPython memory-address noise from `repr` fallbacks so audit rows
# stay diff-friendly across runs: "<orca.X.Y object at 0x...>" -> "<orca.X.Y object>".
_REPR_MEMADDR_RE = re.compile(r" at 0x[0-9A-Fa-f]+>")


def _to_json_safe(value: Any) -> JsonValue:
    """Convert a captured call-arg value to JSON-safe data.

    `@dangerous` captures whatever the caller passed, including rich domain
    objects (`Teachpoint`, `WorkflowTemplate`, dataclass DTOs). Every
    downstream surface -- the in-memory audit ring buffer, the daemon's
    REST/refusal payloads, a hosted deployment's persisted audit store -- needs JSON-safe
    data, not a Python object a JSON encoder chokes on. Structured objects
    (dataclasses, Pydantic models, anything with `to_dict()`) project to
    their field shape so audit consumers can read values like teachpoint
    coordinates back; anything else falls back to a repr.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_json_safe(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _to_json_safe(dataclasses.asdict(value))
    if isinstance(value, BaseModel):
        return _to_json_safe(value.model_dump(mode="json"))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _to_json_safe(to_dict())
    return _REPR_MEMADDR_RE.sub(">", repr(value))


class DangerLevel(Enum):
    """How destructive an action is, and how loudly the CLI prompts.

    - SAFE: read-only or idempotent. Never decorated with @dangerous.
    - OPERATOR: mild destructive; CLI uses y/N default N.
    - CRITICAL: state-mutating on a running execution; CLI uses typed-phrase confirm.
    - PHYSICAL: can damage hardware or corrupt calibration; typed-phrase + requires `reason`.
    """
    SAFE = auto()
    OPERATOR = auto()
    CRITICAL = auto()
    PHYSICAL = auto()


@dataclass(frozen=True)
class ParamSpec:
    """Description of a single parameter for an action or device capability."""
    name: str
    type_name: str
    required: bool
    default: str | None  # repr-style string; None when no default
    description: str


@dataclass(frozen=True)
class ActionDescriptor:
    """Advertised metadata for one dangerous action.

    Returned by `describe_action(name)`. Drives CLI prompts, REST docs,
    MCP tool schemas.
    """
    name: str                                  # e.g. "labware.edit_location"
    danger_level: DangerLevel
    message: str                               # format string bound to call kwargs
    requires_reason: bool
    parameters: tuple[ParamSpec, ...] = field(default_factory=tuple)


class ConfirmationRequired(Exception):
    """Raised when a @dangerous method is called without `confirm=True`.

    Carries the action name, descriptor, and the args the caller passed, so
    the UI can format a prompt without hardcoding copy.
    """

    def __init__(
        self,
        action_name: str,
        descriptor: ActionDescriptor,
        call_args: dict[str, JsonValue],
    ) -> None:
        self.action_name = action_name
        self.descriptor = descriptor
        self.call_args = call_args
        super().__init__(
            f"{action_name} requires confirm=True (danger={descriptor.danger_level.name}): "
            f"{descriptor.message}"
        )


class ActionRegistry:
    """Module-level registry of all @dangerous-decorated methods.

    Populated at import time as decorators run. Lookups go by qualified name
    (e.g. "LabwareFacade.edit_location" -> "labware.edit_location" after a
    short post-processing step done in the facade module that owns the method).
    """

    def __init__(self) -> None:
        self._by_name: dict[str, ActionDescriptor] = {}

    def register(self, descriptor: ActionDescriptor) -> None:
        if descriptor.name in self._by_name:
            raise ValueError(
                f"Action name collision: '{descriptor.name}' is already registered. "
                f"Each @dangerous method must have a unique action name."
            )
        self._by_name[descriptor.name] = descriptor

    def describe(self, name: str) -> ActionDescriptor:
        if name not in self._by_name:
            raise KeyError(f"No action registered with name '{name}'")
        return self._by_name[name]

    def list_all(self) -> list[ActionDescriptor]:
        return list(self._by_name.values())

    def clear(self) -> None:
        """Test helper. NOT for production use."""
        self._by_name.clear()


@dataclass(frozen=True)
class AuditEntry:
    """One successful dangerous-action invocation.

    Written by the @dangerous decorator on every confirmed call. UIs can
    query the ring buffer via `list_audit_entries()` and downstream sinks
    can subscribe to the `orca.audit` logger for persistence.
    """
    timestamp: float                           # unix seconds, time.time()
    action_name: str
    danger_level: DangerLevel
    reason: str | None
    call_args: dict[str, JsonValue]


class AuditTrail:
    """In-memory ring buffer of successful @dangerous invocations.

    Bounded to avoid unbounded growth on long-running daemons. Persistence
    is opt-in via the `orca.audit` logger (see `audit_logger`): wire a file
    handler in the daemon to durably record every entry.
    """

    _MAX_ENTRIES = 1000

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self._entries.append(entry)
        overflow = len(self._entries) - self._MAX_ENTRIES
        if overflow > 0:
            del self._entries[:overflow]

    def list_all(self) -> list[AuditEntry]:
        return list(self._entries)

    def clear(self) -> None:
        """Test helper. NOT for production use."""
        self._entries.clear()


_REGISTRY = ActionRegistry()
_AUDIT_TRAIL = AuditTrail()


def list_audit_entries() -> list[AuditEntry]:
    """Snapshot of the audit trail. UIs use this for operator activity views."""
    return _AUDIT_TRAIL.list_all()


def clear_audit_trail() -> None:
    """Test helper. NOT for production use."""
    _AUDIT_TRAIL.clear()


def describe_action(name: str) -> ActionDescriptor:
    """Look up the descriptor for a registered dangerous action."""
    return _REGISTRY.describe(name)


def list_actions() -> list[ActionDescriptor]:
    """List every registered dangerous action."""
    return _REGISTRY.list_all()


def _build_param_specs(
    func: Callable[..., Any],
    skip: tuple[str, ...] = ("self", "confirm", "reason"),
) -> tuple[ParamSpec, ...]:
    """Extract `ParamSpec`s from a method signature. Skips `self` and kwargs
    that the decorator itself handles (`confirm`, `reason`)."""
    sig = inspect.signature(func)
    specs: list[ParamSpec] = []
    for pname, param in sig.parameters.items():
        if pname in skip:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            type_name = "Any"
        else:
            type_name = getattr(annotation, "__name__", None) or str(annotation)
        has_default = param.default is not inspect.Parameter.empty
        default_repr = repr(param.default) if has_default else None
        specs.append(ParamSpec(
            name=pname,
            type_name=type_name,
            required=not has_default,
            default=default_repr,
            description="",  # future: pull from docstring-per-param parser
        ))
    return tuple(specs)


def dangerous(
    name: str,
    level: DangerLevel,
    message: str,
    requires_reason: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a method as dangerous. Enforces `confirm=True` at call time.

    Args:
        name: unique action name, convention `"<facade>.<method>"` (e.g. "labware.edit_location").
        level: how loudly the UI should prompt.
        message: template string shown in the prompt; `{kwarg}` placeholders
            are formatted with the call's bound arguments.
        requires_reason: if True, caller must pass `reason: str` in addition
            to `confirm=True`. Used for PHYSICAL-level ops that need an audit trail.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        descriptor = ActionDescriptor(
            name=name,
            danger_level=level,
            message=message,
            requires_reason=requires_reason,
            parameters=_build_param_specs(func),
        )
        _REGISTRY.register(descriptor)

        is_coroutine = inspect.iscoroutinefunction(func)

        if is_coroutine:
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                confirm = kwargs.pop("confirm", False)
                reason = kwargs.get("reason")
                if not confirm:
                    bound_args = _bind_call_args(func, args, kwargs)
                    raise ConfirmationRequired(name, descriptor, call_args=bound_args)
                if requires_reason and not reason:
                    raise ValueError(
                        f"{name} requires reason=<str> in addition to confirm=True"
                    )
                result = await func(*args, **kwargs)
                _record_audit(name, level, reason, func, args, kwargs)
                return result
            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            confirm = kwargs.pop("confirm", False)
            reason = kwargs.get("reason")
            if not confirm:
                bound_args = _bind_call_args(func, args, kwargs)
                raise ConfirmationRequired(name, descriptor, bound_args)
            if requires_reason and not reason:
                raise ValueError(
                    f"{name} requires reason=<str> in addition to confirm=True"
                )
            result = func(*args, **kwargs)
            _record_audit(name, level, reason, func, args, kwargs)
            return result
        return sync_wrapper

    return decorator


def _record_audit(
    name: str,
    level: DangerLevel,
    reason: str | None,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """Append an audit entry and emit a log record after a confirmed call."""
    call_args = _bind_call_args(func, args, kwargs)
    call_args.pop("reason", None)
    entry = AuditEntry(
        timestamp=time.time(),
        action_name=name,
        danger_level=level,
        reason=reason,
        call_args=call_args,
    )
    _AUDIT_TRAIL.record(entry)
    audit_logger.info(
        "%s level=%s reason=%r args=%r",
        name, level.name, reason, call_args,
    )


def _bind_call_args(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, JsonValue]:
    """Best-effort bind positional + keyword args to names, JSON-safe.

    Used to format the descriptor's message template, report call args on
    refusal, and populate the audit trail. Every downstream surface needs
    JSON-safe data, so values are converted at capture time via
    `_to_json_safe` rather than carried as raw domain objects.
    """
    try:
        sig = inspect.signature(func)
        bound = sig.bind_partial(*args, **kwargs)
        return {
            k: _to_json_safe(v) for k, v in bound.arguments.items() if k != "self"
        }
    except TypeError:
        # Signature mismatch (unusual). Report kwargs only.
        return {k: _to_json_safe(v) for k, v in kwargs.items()}
