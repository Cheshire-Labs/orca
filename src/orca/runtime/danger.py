"""Uniform safety gating for mutation operations across every UI.

Every runtime mutation that can harm state (hardware, in-flight executions,
calibration history, etc.) is decorated with `@dangerous`. The decorator:

1. Attaches an `ActionDescriptor` to the method (`action_descriptor`).
2. Enforces at call time that the caller pass `confirm=True`. If False, raises
   `ConfirmationRequired` carrying the descriptor so the UI can render
   the right prompt without hardcoding message copy.

An action done outside its facade writes the same audit row with
`record_audit(action_descriptor(method), ...)`.
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

from orca.runtime.audit_store import AUDIT_ENTRY_ATTR, AUDIT_LOGGER_NAME, AuditEntry

audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)

# Strip CPython memory-address noise from `repr` fallbacks so audit rows
# stay diff-friendly across runs: "<orca.X.Y object at 0x...>" -> "<orca.X.Y object>".
_REPR_MEMADDR_RE = re.compile(r" at 0x[0-9A-Fa-f]+>")


def _to_json_safe(value: Any) -> JsonValue:
    """Convert a captured call-arg value to JSON-safe data.

    `@dangerous` captures whatever the caller passed, including rich domain
    objects (`Teachpoint`, `WorkflowTemplate`, dataclass DTOs). Every
    downstream surface -- REST refusal payloads and each server's audit
    store -- needs JSON-safe data, not a Python object a JSON encoder chokes on. Structured objects
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

    Read with `action_descriptor(method)`. A refusal carries it, so the UI
    can render the prompt.
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


_DESCRIPTOR_ATTR = "__orca_action__"


def action_descriptor(method: Callable[..., Any]) -> ActionDescriptor:
    """The descriptor `@dangerous` attached to `method`. KeyError when it has none."""
    descriptor = getattr(method, _DESCRIPTOR_ATTR, None)
    if not isinstance(descriptor, ActionDescriptor):
        raise KeyError(f"{getattr(method, '__qualname__', method)!r} is not a @dangerous action")
    return descriptor


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
            setattr(async_wrapper, _DESCRIPTOR_ATTR, descriptor)
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
        setattr(sync_wrapper, _DESCRIPTOR_ATTR, descriptor)
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
    """Log the audit entry for a confirmed call."""
    call_args = _bind_call_args(func, args, kwargs)
    call_args.pop("reason", None)
    write_audit(name, level, reason, call_args)


def record_audit(
    action: ActionDescriptor, reason: str | None, call_args: dict[str, JsonValue],
) -> None:
    """Write the audit row for a dangerous action done outside its facade, at the action's own level."""
    kept = {k: v for k, v in call_args.items() if k not in ("reason", "confirm")}
    write_audit(action.name, action.danger_level, reason, kept)


def write_audit(
    name: str,
    level: DangerLevel,
    reason: str | None,
    call_args: dict[str, JsonValue],
) -> None:
    """Log one audit row on `orca.audit`, carrying the full entry for the audit store."""
    entry = AuditEntry(
        timestamp=time.time(),
        action_name=name,
        danger_level=level.name,
        reason=reason,
        call_args=call_args,
    )
    audit_logger.info(
        "%s level=%s reason=%r args=%r",
        name, level.name, reason, call_args,
        extra={AUDIT_ENTRY_ATTR: entry},
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
