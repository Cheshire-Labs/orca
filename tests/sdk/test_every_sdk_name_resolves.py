import importlib
import pkgutil

import orca.sdk


def _sdk_modules() -> list[str]:
    return [f"orca.sdk.{info.name}" for info in pkgutil.iter_modules(orca.sdk.__path__)]


def test_every_sdk_module_declares_its_names() -> None:
    for name in _sdk_modules():
        assert isinstance(getattr(importlib.import_module(name), "__all__", None), list), name


def test_every_name_in_an_sdk_all_is_an_attribute_of_that_module() -> None:
    missing: list[str] = []
    for name in _sdk_modules():
        module = importlib.import_module(name)
        missing += [f"{name}.{n}" for n in module.__all__ if not hasattr(module, n)]
    assert missing == []


def test_no_sdk_all_lists_a_name_twice() -> None:
    for name in _sdk_modules():
        names = importlib.import_module(name).__all__
        assert len(names) == len(set(names)), name
