"""Every audited facade verb takes an optional reason, by keyword only.

The audit decorator reads only a keyword `reason`. A verb without one writes
its row with no reason; a verb that also takes it by position drops a reason
passed that way.
"""

import importlib
import inspect
import pkgutil
from collections.abc import Callable, Iterator

import pytest

import orca.runtime.danger as danger
import orca.runtime.facades as facades

# mutate_on_next_pause is engine-internal. A recovery is audited without a
# reason.
_TAKES_NO_REASON = {"ThreadFacade.mutate_on_next_pause", "ThreadFacade.recover"}


def _audited_methods() -> Iterator[tuple[str, Callable[..., None]]]:
    for info in pkgutil.iter_modules(facades.__path__):
        module = importlib.import_module(f"{facades.__name__}.{info.name}")
        for class_name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            for name, attr in vars(cls).items():
                # A decorated method's own code is the decorator's wrapper.
                if inspect.isfunction(attr) and attr.__code__.co_filename == danger.__file__:
                    yield f"{class_name}.{name}", inspect.unwrap(attr)


AUDITED = [m for m in _audited_methods() if m[0] not in _TAKES_NO_REASON]


def test_the_walk_finds_the_audited_verbs() -> None:
    names = {name for name, _ in AUDITED}
    assert {"DeviceFacade.execute", "LabwareFacade.edit_location", "ThreadFacade.pause"} <= names


@pytest.mark.parametrize("name,inner", AUDITED, ids=[name for name, _ in AUDITED])
def test_reason_is_a_keyword_only_parameter(name: str, inner: Callable[..., None]) -> None:
    reason = inspect.signature(inner).parameters.get("reason")
    assert reason is not None, f"{name} takes no reason"
    assert reason.kind is inspect.Parameter.KEYWORD_ONLY, f"{name} takes reason by position"
