"""Unit tests for LabwareTemplate.matches / .is_wildcard.

Promoted from a hosted-side isinstance-dispatch ladder in the thread-mutation
API. The cleanup adds ``matches`` + ``is_wildcard`` to both
``LabwareTemplate`` and ``AnyLabwareTemplate`` so callers compose via
duck-typed interface instead of runtime type dispatch.
"""

from orca.resource_models.labware import (
    AnyLabwareTemplate,
    LabwareInstance,
    LabwareTemplate,
)


class _ConcreteLabwareTemplate(LabwareTemplate):
    """Minimal LabwareTemplate subclass for testing the base behavior."""

    async def create_instance(self) -> LabwareInstance:
        return LabwareInstance(self.name, "test")


def test_concrete_template_matches_by_name() -> None:
    tmpl = _ConcreteLabwareTemplate("plate_1")
    assert tmpl.matches("plate_1") is True
    assert tmpl.matches("plate_2") is False


def test_concrete_template_is_not_wildcard() -> None:
    tmpl = _ConcreteLabwareTemplate("plate_1")
    assert tmpl.is_wildcard is False


def test_any_labware_template_matches_anything() -> None:
    tmpl = AnyLabwareTemplate()
    assert tmpl.matches("plate_1") is True
    assert tmpl.matches("trough_x") is True
    assert tmpl.matches("") is True


def test_any_labware_template_is_wildcard() -> None:
    tmpl = AnyLabwareTemplate()
    assert tmpl.is_wildcard is True
