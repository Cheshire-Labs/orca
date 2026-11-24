"""The budget a command sent over the wire gets: what its request declares, read through the request model."""

import pytest
from cheshire_drivers.command_table import DriverKwargs
from cheshire_drivers.command_timings import collect_command_timings
from cheshire_drivers.interfaces import ILiquidHandlerDriver, IShakerDriver, IStorageDriver
from cheshire_drivers.liquid_handler_models import DispenseRequest, DispenseTarget

from orca.gateway import step_pacer_adapter
from orca.gateway.step_pacer_adapter import wire_budget

SHAKER = frozenset({"IShaker"})
TYPICAL_SHAKE = collect_command_timings(IShakerDriver)["shake"].typical_seconds


def test_a_shake_is_budgeted_its_requested_duration() -> None:
    assert wire_budget(SHAKER, "shake", {"speed": 500, "duration": 120}) == 120.0


def test_params_the_request_model_refuses_fall_back_to_the_commands_typical_time() -> None:
    assert wire_budget(SHAKER, "shake", {"duration": "two minutes"}) == TYPICAL_SHAKE


def test_a_command_no_advertised_interface_declares_has_no_budget() -> None:
    assert wire_budget(SHAKER, "vendor_extra", {}) is None


class _WhichInterface:
    def __init__(self) -> None:
        self.used: list[type] = []

    def __call__(self, driver_cls: type, command: str, kwargs: DriverKwargs) -> float:
        self.used.append(driver_cls)
        return 1.0


def test_a_verb_two_advertised_interfaces_declare_is_budgeted_by_the_one_its_params_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stacker's dispense takes no arguments; a liquid handler's takes the wells and volumes.
    both = frozenset({"ILiquidHandler", "IStorage"})
    pipetting = DispenseRequest(dispenses=[DispenseTarget(labware="plate", positions=["A1"], volumes=[10.0])])
    rule = _WhichInterface()
    monkeypatch.setattr(step_pacer_adapter, "nominal_seconds_for", rule)

    wire_budget(both, "dispense", {})
    wire_budget(both, "dispense", pipetting.model_dump(mode="json"))

    assert rule.used == [IStorageDriver, ILiquidHandlerDriver]


class _WhatArguments:
    def __init__(self) -> None:
        self.kwargs: list[DriverKwargs] = []

    def __call__(self, driver_cls: type, command: str, kwargs: DriverKwargs) -> float:
        self.kwargs.append(dict(kwargs))
        return 1.0


def test_a_command_with_plain_arguments_is_budgeted_with_them(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _WhatArguments()
    monkeypatch.setattr(step_pacer_adapter, "nominal_seconds_for", rule)

    wire_budget(frozenset({"ITempSettable"}), "set_temperature", {"temperature": 37.0})

    assert rule.kwargs == [{"temperature": 37.0}]


def test_a_property_read_has_no_budget() -> None:
    assert wire_budget(SHAKER, "is_initialized", {}) is None
