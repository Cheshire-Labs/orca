"""Every device gets the same disconnect grace: 300 s. Only tests pass another value."""

import pytest

from orca.gateway.controller.disconnect_grace import DisconnectGrace


def test_default_grace_is_300s() -> None:
    assert DisconnectGrace().seconds == 300.0


def test_custom_grace_honored() -> None:
    assert DisconnectGrace(seconds=120.0).seconds == 120.0


def test_zero_grace_rejected() -> None:
    with pytest.raises(ValueError):
        DisconnectGrace(seconds=0.0)


def test_negative_grace_rejected() -> None:
    with pytest.raises(ValueError):
        DisconnectGrace(seconds=-1.0)
